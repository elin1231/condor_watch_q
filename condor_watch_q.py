#!/usr/bin/python

# Copyright 2020 HTCondor Team, Computer Sciences Department,
# University of Wisconsin-Madison, WI.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import print_function

import argparse
import collections
import getpass
import itertools
import sys
import textwrap
import enum
import datetime
import os
import time
import operator
import re
import contextlib
import shutil
import struct

import htcondor
import classad

try:
    from colorama import init

    init()
    HAS_COLORAMA = True
except ImportError:
    HAS_COLORAMA = False

IS_WINDOWS = os.name == "nt"


def parse_args():
    parser = argparse.ArgumentParser(
        prog="condor_watch_q",
        description=textwrap.dedent(
            """
            Track the status of jobs over time without repeatedly querying the condor_schedd.

            If no users, cluster ids, event logs, or batch names are given,
            condor_watch_q will default to tracking all of the current user's jobs.

            By default, condor_watch_q will never exit on its own 
            (unless it encounters an error or it is not tracking any jobs).
            You can tell it to exit when certain conditions are met. For example,
            to exit with status 0 when all of the jobs it is tracking are done
            or with status 1 when any job is held, you could run

                condor_watch_q -exit all,done,0 -exit any,held,1
            """
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )

    # GENERAL OPTIONS

    parser.add_argument(
        "-help",
        action="help",
        default=argparse.SUPPRESS,
        help="Show this help message and exit.",
    )

    parser.add_argument(
        "-debug", action="store_true", help="Turn on HTCondor debug printing."
    )
    # TRACKING OPTIONS

    parser.add_argument(
        "-users", nargs="+", metavar="USER", help="Which users to track."
    )
    parser.add_argument(
        "-clusters", nargs="+", metavar="CLUSTER_ID", help="Which cluster IDs to track."
    )
    parser.add_argument(
        "-files", nargs="+", metavar="FILE", help="Which event logs to track."
    )
    parser.add_argument(
        "-batches", nargs="+", metavar="BATCH_NAME", help="Which batch names to track."
    )

    parser.add_argument(
        "-collector",
        action="store",
        nargs=1,
        default=None,
        help="Which collector to contact to find the schedd, if needed. Defaults to the local collector.",
    )
    parser.add_argument(
        "-schedd",
        action="store",
        nargs=1,
        default=None,
        help="Which schedd to contact for queries, if needed. Defaults to the local schedd.",
    )

    # BEHAVIOR OPTIONS

    parser.add_argument(
        "-exit",
        action=ExitConditions,
        metavar="GROUPER,JOB_STATUS[,EXIT_STATUS]",
        help=textwrap.dedent(
            """
            Specify conditions under which condor_watch_q should exit. 

            GROUPER is one of {{{exit_groupers}}}.

            JOB_STATUS is one of {{{job_statuses}}}.

            The "active" status means "in the queue",
            and includes jobs in the idle, running, and held states.

            ``EXIT_STATUS`` may be any valid exit status integer.

            To specify multiple exit conditions, pass this option multiple times.
            condor_watch_q will exit when any of the conditions are satisfied.
            """.format(
                exit_groupers=", ".join(EXIT_GROUPERS.keys()),
                job_statuses=", ".join(EXIT_JOB_STATUS_CHECK.keys()),
            )
        ),
    )

    # DISPLAY OPTIONS

    parser.add_argument(
        "-groupby",
        action="store",
        default="batch",
        choices=("batch", "log", "cluster"),
        help=textwrap.dedent(
            """
            Select what attribute to group jobs by.

            Note that batch names can only be determined if the tracked jobs were
            found in the queue; if they were not, a default batch name is used.
            """
        ),
    )
    parser.add_argument(
        "-table",
        "-no-table",
        action=NegateAction,
        nargs=0,
        default=True,
        help="Enable/disable the table. Enabled by default.",
    )
    parser.add_argument(
        "-progress",
        "-no-progress",
        action=NegateAction,
        nargs=0,
        default=True,
        help="Enable/disable the progress bar. Enabled by default.",
    )
    parser.add_argument(
        "-row-progress",
        "-no-row-progress",
        action=NegateAction,
        nargs=0,
        default=True,
        help="Enable/disable the progress bar for each row. Enabled by default.",
    )
    parser.add_argument(
        "-summary",
        "-no-summary",
        action=NegateAction,
        nargs=0,
        default=True,
        help="Enable/disable the summary line. Enabled by default.",
    )
    parser.add_argument(
        "-summary-type",
        action="store",
        choices=("totals", "percentages"),
        default="totals",
        help="Choose what to display on the summary line. By default, show totals.",
    )
    parser.add_argument(
        "-updated-at",
        "-no-updated-at",
        action=NegateAction,
        nargs=0,
        default=True,
        help='Enable/disable the "updated at" line. Enabled by default.',
    )
    parser.add_argument(
        "-abbreviate",
        "-no-abbreviate",
        action=NegateAction,
        nargs=0,
        default=True,
        help="Enable/disable abbreviating path components to the shortest somewhat-unique prefix. Disabled by default.",
    )
    parser.add_argument(
        "-color",
        "-no-color",
        action=NegateAction,
        nargs=0,
        default=sys.stdout.isatty() and (not IS_WINDOWS or HAS_COLORAMA),
        help="Enable/disable colored output. Enabled by default if connected to a tty. Disabled on Windows if colorama is not available.",
    )
    parser.add_argument(
        "-refresh",
        "-no-refresh",
        action=NegateAction,
        nargs=0,
        default=sys.stdout.isatty(),
        help="Enable/disable refreshing output (instead of appending). Enabled by default if connected to a tty.",
    )
    parser.add_argument("-dag", dest="dag", action="store_true")
    parser.add_argument("-no-dag", dest="dag", action="store_false")
    # This keeps returning false, reverted to adding seperately
    # parser.add_argument(
    #     "-dag",
    #     "-no-dag",
    #     action=NegateAction,
    #     nargs=0,
    #     default=False,
    #     help="Enable/disable tracking dag jobs. Disabled by default.",
    # )

    args, unknown = parser.parse_known_args()
    print(args)
    if len(unknown) != 0:
        check_unknown_args_for_known_errors(parser, unknown)

        # because we're in this branch, this will fail, printing out the
        # original errors argparse would have printed for the unknown args
        # if we didn't catch them ourselves
        args = parser.parse_args()

    args.groupby = {
        "log": "event_log_path",
        "cluster": "cluster_id",
        "batch": "batch_name",
    }[args.groupby]
    return args


def check_unknown_args_for_known_errors(parser, unknown_args):
    """
    This function tries to capture known errors in the arguments that
    argparse fails to parse and emit our own error messages.
    Individual args are passed to _check_unknown_arg for processing.
    Common errors are trying to use condor_q-like arguments.
    """
    unknown_args = iter(unknown_args)
    for arg in unknown_args:
        error_message = _check_unknown_arg(arg, unknown_args)
        if error_message is None:
            continue

        parser.print_usage()
        print(
            "{}: error: argument {}: {}".format(parser.prog, arg, error_message),
            file=sys.stderr,
        )
        sys.exit(1)


def _check_unknown_arg(arg, unknown_args):
    if arg.isdigit():
        cluster_ids = [arg]
        for next_arg in unknown_args:
            if next_arg.startswith("-"):
                break
            if not next_arg.isdigit():
                continue
            cluster_ids.append(next_arg)

        return "to track specific cluster IDs, try -clusters {}".format(
            " ".join(cluster_ids)
        )

    elif "-totals" in arg:
        return "to only print totals, try -no-table"

    elif "-userlog" in arg:
        files = []
        for next_arg in unknown_args:
            if next_arg.startswith("-"):
                break
            files.append(next_arg)

        return "to track jobs from specific event logs, try -files {}".format(
            " ".join(files)
        )

    elif "-nobatch" in arg:
        return "to group by something other than batch name, try -groupby {{batch,log,cluster}}"

    elif not arg.startswith("-"):
        users = [arg]
        for next_arg in unknown_args:
            if next_arg.startswith("-"):
                break
            if next_arg.isdigit():
                continue
            users.append(next_arg)

        return "to track jobs from specific users, try -users {}".format(
            " ".join(users)
        )


class ExitConditions(argparse.Action):
    def __call__(self, parser, args, values, option_string=None):
        v = values.split(",")
        if len(v) == 3:
            grouper, status, exit_status = v
        elif len(v) == 2:
            grouper, status = v
            exit_status = 0
        else:
            parser.error(message="invalid -exit specification")

        if grouper not in EXIT_GROUPERS:
            parser.error(
                message='invalid GROUPER "{grouper}", must be one of {{{groupers}}}'.format(
                    grouper=grouper, groupers=", ".join(EXIT_GROUPERS.keys())
                )
            )

        if status not in EXIT_JOB_STATUS_CHECK:
            parser.error(
                message='invalid JOB_STATUS "{status}", must be one of {{{statuses}}}'.format(
                    status=status, statuses=", ".join(EXIT_JOB_STATUS_CHECK.keys())
                )
            )

        try:
            exit_status = int(exit_status)
        except ValueError:
            parser.error(
                message='exit_status must be an integer, but was "{}"'.format(
                    exit_status
                )
            )

        if getattr(args, self.dest, None) is None:
            setattr(args, self.dest, [])

        getattr(args, self.dest).append((grouper, status, exit_status))


class NegateAction(argparse.Action):
    def __call__(self, parser, args, values, option_string=None):
        setattr(args, self.dest, option_string.rstrip("-").startswith("no"))


def cli():
    args = parse_args()

    if args.debug:
        warning("Debug mode enabled...")
        htcondor.enable_debug()

    try:
        return watch_q(
            users=args.users,
            cluster_ids=args.clusters,
            event_logs=args.files,
            batches=args.batches,
            collector=args.collector,
            schedd=args.schedd,
            exit_conditions=args.exit,
            group_by=args.groupby,
            table=args.table,
            progress_bar=args.progress,
            row_progress_bar=args.row_progress,
            summary=args.summary,
            summary_type=args.summary_type,
            updated_at=args.updated_at,
            color=args.color,
            refresh=args.refresh,
            abbreviate_path_components=args.abbreviate,
            dag=args.dag,
        )
    except Exception as e:
        if args.debug:
            raise

        error(
            "Unhandled error: {}. Re-run with -debug for a full stack trace.".format(e)
        )
        sys.exit(1)


EXIT_GROUPERS = {"all": all, "any": any, "none": lambda _: not any(_)}
EXIT_JOB_STATUS_CHECK = {
    "active": lambda s: s in ACTIVE_STATES,
    "done": lambda s: s is JobStatus.COMPLETED,
    "idle": lambda s: s is JobStatus.IDLE,
    "held": lambda s: s is JobStatus.HELD,
}

TOTAL = "TOTAL"
ACTIVE_JOBS = "JOB_IDS"
EVENT_LOG = "LOG"
CLUSTER_ID = "CLUSTER"
BATCH_NAME = "BATCH"

# attribute is the Python attribute name of the Cluster object
# key is the key in the events and job rows
GROUPBY_ATTRIBUTE_TO_AD_KEY = {
    "event_log_path": EVENT_LOG,
    "cluster_id": CLUSTER_ID,
    "batch_name": BATCH_NAME,
}
GROUPBY_AD_KEY_TO_ATTRIBUTE = {v: k for k, v in GROUPBY_ATTRIBUTE_TO_AD_KEY.items()}


def watch_q(
    users=None,
    cluster_ids=None,
    event_logs=None,
    batches=None,
    collector=None,
    schedd=None,
    exit_conditions=None,
    group_by="batch_name",
    table=True,
    progress_bar=True,
    row_progress_bar=True,
    summary=True,
    summary_type="totals",
    updated_at=True,
    color=True,
    refresh=True,
    abbreviate_path_components=False,
    dag=False,
):
    if users is None and cluster_ids is None and event_logs is None and batches is None:
        users = [getpass.getuser()]
    if exit_conditions is None:
        exit_conditions = []

    key = GROUPBY_ATTRIBUTE_TO_AD_KEY[group_by]

    row_fmt = (lambda s, r: colorize(s, determine_row_color(r))) if color else None

    (
        cluster_ids,
        event_logs,
        batch_names,
        dagman_clusters_to_path,
    ) = find_job_event_logs(
        users, cluster_ids, event_logs, batches, collector=collector, schedd=schedd
    )
    if len(event_logs) == 0:
        warning("No jobs found, exiting...")
        sys.exit(0)

    tracker = JobStateTracker(event_logs, batch_names)

    exit_checks = []
    for grouper, checker, exit_status in exit_conditions:
        disp = "{} {}".format(grouper, checker)
        exit_grouper = EXIT_GROUPERS[grouper.lower()]
        exit_check = EXIT_JOB_STATUS_CHECK[checker.lower()]
        exit_checks.append((exit_grouper, exit_check, exit_status, disp))

    try:
        msg, move, clear = None, None, None

        while True:
            with display_temporary_message("Processing new events...", enabled=refresh):
                processing_messages = tracker.process_events()
                now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                # "if msg is not None" skips the first iteration, when there's nothing to clear
                if msg is not None and refresh:
                    msg = strip_ansi(msg)
                    prev_lines = list(msg.splitlines())
                    prev_len_lines = [len(line) for line in prev_lines]
                    move = "\033[{}A\r".format(len(prev_len_lines))
                    clear = "\n".join(" " * len(line) for line in prev_lines) + "\n"

                if dag:
                    groups_by_key = group_jobs_by_dag(
                        tracker.clusters, key, dagman_clusters_to_path, batch_names
                    )
                else:
                    groups_by_key = group_clusters_by_key(tracker.clusters, key)

                rows_by_key, totals = make_rows_from_groups(groups_by_key, key)

                headers, rows_by_key = strip_empty_columns(rows_by_key)

                # strip out 0 values
                rows_by_key = {
                    key: {k: v for k, v in row.items() if v != 0}
                    for key, row in rows_by_key.items()
                }

                if key == EVENT_LOG and abbreviate_path_components:
                    for row in rows_by_key.values():
                        row[key] = abbreviate_path(row[key])

                rows_by_key = sorted(
                    rows_by_key.items(),
                    key=lambda key_row: min(
                        cluster.cluster_id for cluster in groups_by_key[key_row[0]]
                    ),
                )

                msg = []

                terminal_columns, terminal_rows = get_terminal_size()

                if table:
                    msg += make_table(
                        row_progress_bar=row_progress_bar,
                        terminal_columns=terminal_columns,
                        headers=[key] + headers,
                        rows=[row for _, row in rows_by_key],
                        row_fmt=row_fmt,
                        alignment=TABLE_ALIGNMENT,
                        fill="-",
                    )
                    msg += [""]

                if progress_bar:
                    msg += make_progress_bar(
                        totals=totals, width=terminal_columns, color=color
                    )
                    msg += [""]

                if summary:
                    if summary_type == "totals":
                        msg += make_summary_with_totals(totals, width=terminal_columns)
                    elif summary_type == "percentages":
                        msg += make_summary_with_percentages(
                            totals, width=terminal_columns
                        )
                    msg += [""]

                if updated_at:
                    msg += ["Updated at {}".format(now)] + [""]

                if len(msg) > terminal_rows:
                    msg = msg[: terminal_rows - 1]
                    msg = (
                        "\n".join(msg[:-1])
                        + "\nInsufficient terminal height to display full output!"
                    )
                else:
                    # msg[:-1] because we need to strip the last blank section delimiter line off
                    msg = "\n".join(msg[:-1])

                if not refresh:
                    msg += "\n..."

                if not exit_conditions:
                    msg += "\nInput ^c to exit"

            if clear and move:
                sys.stdout.write(move + clear + move)
                sys.stdout.flush()

            if len(processing_messages) > 0:
                print(
                    "\n".join("{} | {}".format(now, m) for m in processing_messages),
                    file=sys.stderr,
                )

            print(msg)

            for grouper, checker, exit_status, disp in exit_checks:
                if grouper((checker(s) for s in tracker.job_states)):
                    print(
                        'Exiting with code {} because of condition "{}" at {}'.format(
                            exit_status, disp, now
                        )
                    )
                    sys.exit(exit_status)

            time.sleep(2)
    except KeyboardInterrupt:
        sys.exit(0)


terminal_size = collections.namedtuple("terminal_size", ["columns", "lines"])


def get_terminal_size():
    try:
        return shutil.get_terminal_size()
    # TODO: remove this once we don't support Python 2 anymore
    except AttributeError:  # Python 2 doesn't have shutil.get_terminal_size
        try:
            if IS_WINDOWS:
                # https://stackoverflow.com/questions/566746/how-to-get-linux-console-window-width-in-python
                from ctypes import windll, create_string_buffer

                h = windll.kernel32.GetStdHandle(-12)
                csbi = create_string_buffer(22)
                res = windll.kernel32.GetConsoleScreenBufferInfo(h, csbi)
                if res:
                    (_, _, _, _, _, left, top, right, bottom, _, _,) = struct.unpack(
                        "hhhhHhhhhhh", csbi.raw
                    )
                    return terminal_size(right - left + 1, bottom - top + 1)
                else:
                    raise OSError("Failed to get Windows terminal size")
            else:
                rows, cols = os.popen("stty size", "r").read().split()
                return terminal_size(int(cols), int(rows))
        except Exception:
            return terminal_size(80, 24)


@contextlib.contextmanager
def display_temporary_message(msg, enabled=True):
    """Display a single-line message until the context ends."""
    if enabled:
        print(msg, end="")
        sys.stdout.flush()

        yield

        print("\r{}\r".format(" " * len(msg)), end="")
        sys.stdout.flush()
    else:
        yield


# get_schedd would only return these attributes
# add to this so that identify if dagman job, if dagman, query again to get information on that job (dag specific information, pipe that to system)
# Find nodes log
# Collapse all cluster appear in query, associate with dag instead,
# Every job in dag has special (submit event node, belong to certian dag node)
# right now:
# DAGManJobId for projection

# add how many jobs are in the cluster
# parse env to get _CONDOR_DAGMAN_LOG for finding number of jobs in dag
PROJECTION = [
    "ClusterId",
    "Owner",
    "UserLog",
    "DAGManNodesLog",
    "JobBatchName",
    "Iwd",
    "DAG_NodesTotal",
]
def find_job_event_logs(
    users=None,
    cluster_ids=None,
    files=None,
    batches=None,
    dagman_ids=None,
    collector=None,
    schedd=None,
):
    """
    Discover job event logs to read events from based on various methods.

    Parameters
    ----------
    users
        Find job event logs for these user's active jobs.
    cluster_ids
        Find job event logs for these clusters.
    files
        Find these job event logs (basically, these just get passed straight through).
    batches
        Find job event logs for these batch names.
    collector
        Query this collector to find the schedd.
        Defaults to the local collector.
    schedd
        Query this schedd for users, cluster_ids, and batches.
        Defaults to the local schedd.
    """
    if users is None:
        users = []
    if cluster_ids is None:
        cluster_ids = []
    if files is None:
        files = []
    if batches is None:
        batches = []

    clusters = set()
    event_logs = set()
    batch_names = {}
    dagman_node_nums = set()
    already_warned_missing_log = set()
    dagman_clusters_to_path = {}

    for file in files:
        event_logs.add(os.path.abspath(file))

    constraint = " || ".join(
        itertools.chain(
            ("Owner == {}".format(classad.quote(u)) for u in users),
            ("ClusterId == {}".format(cid) for cid in cluster_ids),
            ("JobBatchName == {}".format(b) for b in batches),
        )
    )

    for ad in get_ads(constraint, collector, schedd):
        cluster_id = ad["ClusterId"]
        clusters.add(cluster_id)

        batch_names[cluster_id] = ad.get("JobBatchName")

        try:
            log_path = ad["UserLog"]
        except KeyError:
            if cluster_id not in already_warned_missing_log:
                warning(
                    "Cluster {} does not have a job event log file (set log=<path> in the submit description)".format(
                        cluster_id
                    )
                )
                already_warned_missing_log.add(cluster_id)
            continue
        # if the path is not absolute, try to make it absolute using the
        # job's initial working directory

        if "DAGManNodesLog" in ad:
            dagman_clusters_to_path[cluster_id] = ad["DAGManNodesLog"]
            log_path = ad["DAGManNodesLog"]
        else:
            if not os.path.isabs(log_path):
                log_path = os.path.abspath(os.path.join(ad["Iwd"], log_path))

        event_logs.add(log_path)

        try:
            node_num = ad["DAG_NodesTotal"]
            dagman_node_nums.add(node_num)
        except KeyError:
            continue

    return (clusters, event_logs, batch_names, dagman_clusters_to_path)


def get_ads(constraint, collector, schedd):
    if constraint == "":
        return []

    try:
        return get_schedd(collector=collector, schedd=schedd).query(
            constraint, PROJECTION
        )
    except Exception as e:
        warning("Was not able to query to discover job event logs due to: {}".format(e))
        return []


def get_schedd(collector=None, schedd=None):
    if collector is None and schedd is None:
        schedd = htcondor.Schedd()
    else:
        coll = htcondor.Collector(collector)
        schedd_ad = coll.locate(htcondor.DaemonTypes.Schedd, schedd)
        schedd = htcondor.Schedd(schedd_ad)

    return schedd


# tracks state, (group by cluster id)
class Cluster:
    """Holds the job state for a singe cluster."""

    def __init__(self, cluster_id, event_log_path, batch_name):
        self.cluster_id = cluster_id
        self.event_log_path = event_log_path
        self._batch_name = batch_name

        self.job_to_state = {}

    @property
    def batch_name(self):
        return self._batch_name or "ID: {}".format(self.cluster_id)

    def __setitem__(self, key, value):
        self.job_to_state[key] = value

    def __getitem__(self, item):
        return self.job_to_state[item]

    def items(self):
        return self.job_to_state.items()

    def __iter__(self):
        return iter(self.items())


# keeps track of clusters
class JobStateTracker:
    """
    Tracks the job state from many event logs,
    maintaining a mapping of cluster_id to Cluster
    (available as the state attribute).
    """

    def __init__(self, event_log_paths, batch_names):
        event_readers = {}
        for event_log_path in event_log_paths:
            try:
                reader = htcondor.JobEventLog(event_log_path).events(0)
                event_readers[event_log_path] = reader
            except (OSError, IOError) as e:
                warning(
                    "Could not open event log at {} for reading, so it will be ignored. Reason: {}".format(
                        event_log_path, e
                    )
                )

        self.event_readers = event_readers
        self.state = collections.defaultdict(lambda: collections.defaultdict(dict))

        self.batch_names = batch_names

        self.cluster_id_to_cluster = {}

    def process_events(self):
        """
        Process all currently-available events in all event logs.
        """
        messages = []

        for event_log_path, events in self.event_readers.items():
            while True:
                try:
                    event = next(events)
                except StopIteration:
                    break
                except Exception as e:
                    messages.append(
                        fmt_error(
                            "Failed to parse event from {}. Reason: {}".format(
                                event_log_path, e
                            )
                        )
                    )
                    continue

                new_status = JOB_EVENT_STATUS_TRANSITIONS.get(event.type, None)
                if new_status is None:
                    continue

                cluster = self.cluster_id_to_cluster.setdefault(
                    event.cluster,
                    Cluster(
                        cluster_id=event.cluster,
                        event_log_path=event_log_path,
                        batch_name=self.batch_names.get(event.cluster),
                    ),
                )

                cluster[event.proc] = new_status

        return messages

    @property
    def clusters(self):
        return self.cluster_id_to_cluster.values()

    @property
    def job_states(self):
        for cluster in self.clusters:
            for job, state in cluster:
                yield state


def make_rows_from_groups(groups, key):
    totals = collections.defaultdict(int)
    rows = {}

    for attribute_value, clusters in groups.items():
        row_data = row_data_from_job_state(clusters)

        totals[TOTAL] += row_data[TOTAL]
        for status in JobStatus:
            totals[status] += row_data[status]

        if key == EVENT_LOG:
            row_data[key] = nice_path(attribute_value)
        else:
            row_data[key] = attribute_value

        rows[attribute_value] = row_data

    return rows, totals


class Color(str, enum.Enum):
    BLACK = "\033[30m"
    RED = "\033[31m"
    BRIGHT_RED = "\033[31;1m"
    GREEN = "\033[32m"
    BRIGHT_GREEN = "\033[32;1m"
    YELLOW = "\033[33m"
    BRIGHT_YELLOW = "\033[33;1m"
    BLUE = "\033[34m"
    BRIGHT_BLUE = "\033[34;1m"
    MAGENTA = "\033[35m"
    BRIGHT_MAGENTA = "\033[35;1m"
    CYAN = "\033[36m"
    BRIGHT_CYAN = "\033[36;1m"
    WHITE = "\033[37m"
    BRIGHT_WHITE = "\033[37;1m"
    RESET = "\033[0m"


def colorize(string, color):
    return color + string + Color.RESET


def determine_row_color(row):
    if row.get(JobStatus.HELD, 0) > 0:
        return Color.RED
    elif row.get(JobStatus.COMPLETED) == row.get("TOTAL"):
        return Color.GREEN
    elif row.get(JobStatus.RUNNING, 0) > 0:
        return Color.CYAN
    elif row.get(JobStatus.IDLE, 0) > 0:
        return Color.YELLOW
    else:
        return Color.BRIGHT_WHITE

def group_clusters_by_key(clusters, key):
    getter = operator.attrgetter(GROUPBY_AD_KEY_TO_ATTRIBUTE[key])
    groups = collections.defaultdict(list)

    for cluster in clusters:
        groups[getter(cluster)].append(cluster)

    return groups


# assume only dags
def group_jobs_by_dag(clusters, key, dagman_clusters_to_path, batch_names):
    groups = collections.defaultdict(list)

    # reverse dictionary
    dagman_path_to_clusters = {v: k for k, v in dagman_clusters_to_path.items()}

    for cluster in clusters:
        if cluster.event_log_path in dagman_path_to_clusters:
            key = dagman_path_to_clusters[cluster.event_log_path]
            groups[batch_names[key]].append(cluster)

    return groups


def strip_empty_columns(rows_by_key):
    dont_include = set()
    for h in HEADERS:
        if all((row[h] == 0 for row in rows_by_key.values())):
            dont_include.add(h)
    dont_include -= ALWAYS_INCLUDE
    headers = [h for h in HEADERS if h not in dont_include]
    for row_data in dont_include:
        for row in rows_by_key.values():
            row.pop(row_data)

    return headers, rows_by_key


def row_data_from_job_state(clusters):
    """Construct the data for a single row."""
    row_data = {js: 0 for js in JobStatus}
    active_job_ids = []

    for cluster in clusters:
        for proc_id, job_state in cluster:
            row_data[job_state] += 1

            if job_state in ACTIVE_STATES:
                active_job_ids.append("{}.{}".format(cluster.cluster_id, proc_id))

    row_data[TOTAL] = sum(row_data.values())
    active_job_ids.sort(key=lambda jobid: jobid.split("."))

    if len(active_job_ids) > 2:
        active_job_ids = [active_job_ids[0], active_job_ids[-1]]
        row_data[ACTIVE_JOBS] = " ... ".join(active_job_ids)
    else:
        row_data[ACTIVE_JOBS] = ", ".join(active_job_ids)

    return row_data


def nice_path(path):
    """
    Try to find a "nice" representation of a path.
    The shortest (by string length) of these options is returned:
      - the absolute path
      - the path relative to the current user's home directory
      - the path relative to the current working directory
    """
    possibilities = []

    abs = os.path.abspath(path)
    home_dir = os.path.expanduser("~")
    cwd = os.getcwd()

    possibilities.append(abs)

    relative_to_user_home = os.path.relpath(path, home_dir)
    possibilities.append(os.path.join("~", relative_to_user_home))

    relative_to_cwd = os.path.relpath(path, cwd)
    if not relative_to_cwd.startswith("../"):  # i.e, it is not *above* the cwd
        possibilities.append(os.path.join(".", relative_to_cwd))

    return min(possibilities, key=len)


def abbreviate_path(path):
    """
    Abbreviate a filesystem path by finding somewhat-unique prefixes for each
    component in the path.

    If the path was /home/foobar/wizbang/events.log and there was nothing else in any
    of those directories, the result would be /h/f/w/events.log . If there was a
    directory /home/foobar/wizard, the result would be /h/f/wizb/events.log .
    """
    abbreviated_components = []
    path_so_far = ""
    components = split_all(path)
    for component in components[:-1]:
        # the actual, unabbreviated path to this depth in the path
        path_so_far = os.path.expanduser(os.path.join(path_so_far, component))

        # these have special meaning, so don't do anything to them
        if component in ("~", "."):
            abbreviated_components.append(component)
            continue

        contents = os.listdir(os.path.dirname(path_so_far))
        if len(contents) == 1:
            longest_common = ""
        else:
            longest_common = os.path.commonprefix(contents)

        # tack on the common prefix plus one extra character to give a degree of uniqueness
        abbreviated_components.append(component[: len(longest_common) + 1])

    # tack on the last path component as-is; this is the actual event log file name
    abbreviated_components.append(components[-1])

    return os.path.join(*abbreviated_components)


def split_all(path):
    """Consistently split a path into components."""
    split_parts = []
    while 1:
        parts = os.path.split(path)
        if parts[0] == path:  # sentinel for absolute paths
            split_parts.insert(0, parts[0])
            break
        elif parts[1] == path:  # sentinel for relative paths
            split_parts.insert(0, parts[1])
            break
        else:
            path = parts[0]
            split_parts.insert(0, parts[1])
    return split_parts


class JobStatus(enum.Enum):
    REMOVED = "REMOVED"
    HELD = "HELD"
    IDLE = "IDLE"
    RUNNING = "RUN"
    COMPLETED = "DONE"
    TRANSFERRING_OUTPUT = "TRANSFERRING_OUTPUT"
    SUSPENDED = "SUSPENDED"

    def __str__(self):
        return self.value

    @classmethod
    def ordered(cls):
        return (
            cls.REMOVED,
            cls.HELD,
            cls.SUSPENDED,
            cls.IDLE,
            cls.RUNNING,
            cls.TRANSFERRING_OUTPUT,
            cls.COMPLETED,
        )


ACTIVE_STATES = {
    JobStatus.IDLE,
    JobStatus.RUNNING,
    JobStatus.TRANSFERRING_OUTPUT,
    JobStatus.HELD,
    JobStatus.SUSPENDED,
}

ALWAYS_INCLUDE = {
    JobStatus.IDLE,
    JobStatus.RUNNING,
    JobStatus.COMPLETED,
    EVENT_LOG,
    TOTAL,
}

TABLE_ALIGNMENT = {
    EVENT_LOG: "ljust",
    CLUSTER_ID: "ljust",
    TOTAL: "rjust",
    ACTIVE_JOBS: "ljust",
    BATCH_NAME: "ljust",
}
for k in JobStatus:
    TABLE_ALIGNMENT[k] = "rjust"

HEADERS = list(JobStatus.ordered()) + [TOTAL, ACTIVE_JOBS]

JOB_EVENT_STATUS_TRANSITIONS = {
    htcondor.JobEventType.SUBMIT: JobStatus.IDLE,
    htcondor.JobEventType.JOB_EVICTED: JobStatus.IDLE,
    htcondor.JobEventType.JOB_UNSUSPENDED: JobStatus.IDLE,
    htcondor.JobEventType.JOB_RELEASED: JobStatus.IDLE,
    htcondor.JobEventType.SHADOW_EXCEPTION: JobStatus.IDLE,
    htcondor.JobEventType.JOB_RECONNECT_FAILED: JobStatus.IDLE,
    htcondor.JobEventType.JOB_TERMINATED: JobStatus.COMPLETED,
    htcondor.JobEventType.EXECUTE: JobStatus.RUNNING,
    htcondor.JobEventType.JOB_HELD: JobStatus.HELD,
    htcondor.JobEventType.JOB_SUSPENDED: JobStatus.SUSPENDED,
    htcondor.JobEventType.JOB_ABORTED: JobStatus.REMOVED,
}


def make_table(
    row_progress_bar,
    terminal_columns,
    headers,
    rows,
    fill="",
    header_fmt=None,
    row_fmt=None,
    alignment=None,
    color=True,
):
    if header_fmt is None:
        header_fmt = lambda _: _
    if row_fmt is None:
        row_fmt = lambda _a, _b: _a
    if alignment is None:
        alignment = {}

    headers = tuple(headers)
    lengths = [len(str(h)) for h in headers]
    align_methods = [alignment.get(h, "center") for h in headers]
    processed_rows = []

    for row in rows:
        processed_rows.append([str(row.get(key, fill)) + " " for key in headers])
    for row in processed_rows:
        lengths = [max(curr, len(entry)) for curr, entry in zip(lengths, row)]

    header = header_fmt(
        "  ".join(
            getattr(str(h), a)(l) for h, l, a in zip(headers, lengths, align_methods)
        ).rstrip()
    )

    lines = [
        row_fmt(
            "  ".join(
                getattr(f, a)(l)
                for f, l, a in zip(processed_row, lengths, align_methods)
            ),
            original_row,
        )
        for original_row, processed_row in zip(rows, processed_rows)
    ]

    try:
        remaining_columns = terminal_columns - len(strip_ansi(lines[0]))
    except IndexError:
        remaining_columns = 0

    if row_progress_bar and remaining_columns > 10:
        for i, row in zip(range(len(lines)), rows):
            lines[i] += "".join(make_progress_bar(row, remaining_columns, color=color))
    else:
        # Iterate through every row in table, truncate to console width
        for row in range(len(lines)):
            lines[row] = lines[row][:terminal_columns]

    return [header] + lines


ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def strip_ansi(string):
    return ANSI_ESCAPE_RE.sub("", string)


PROGRESS_BAR_CHARS_AND_COLORS = {
    JobStatus.COMPLETED: (Color.GREEN, "#"),
    JobStatus.RUNNING: (Color.CYAN, "="),
    JobStatus.IDLE: (Color.BRIGHT_YELLOW, "-"),
    JobStatus.HELD: (Color.RED, "!"),
    JobStatus.SUSPENDED: (Color.RED, "!"),
    JobStatus.REMOVED: (Color.RED, "!"),
}

PROGRESS_BAR_ORDER = (
    JobStatus.COMPLETED,
    JobStatus.RUNNING,
    JobStatus.IDLE,
    JobStatus.HELD,
    JobStatus.SUSPENDED,
    JobStatus.REMOVED,
)


def make_progress_bar(totals, width=None, color=True):
    width = min(width, 79) - 2  # account for the wrapping [ ]
    num_total = float(totals[TOTAL])

    fractions = [
        safe_divide(n, num_total)
        for n in (
            totals.get(JobStatus.COMPLETED),
            totals.get(JobStatus.RUNNING),
            totals.get(JobStatus.IDLE),
            totals.get(JobStatus.HELD),
            totals.get(JobStatus.SUSPENDED),
            totals.get(JobStatus.REMOVED),
        )
    ]

    bar_section_lengths = [int(width * f) for f in fractions]

    # give any rounded-off space to the longest part of the bar
    bar_section_lengths[argmax(bar_section_lengths)] += width - sum(bar_section_lengths)

    bar = "[{}]".format(
        "".join(
            colorize(
                PROGRESS_BAR_CHARS_AND_COLORS[status][1] * length,
                PROGRESS_BAR_CHARS_AND_COLORS[status][0],
            )
            if color
            else PROGRESS_BAR_CHARS_AND_COLORS[status][1] * length
            for status, length in zip(PROGRESS_BAR_ORDER, bar_section_lengths)
        )
    )

    return [bar]


def argmax(collection):
    """
    Return the index of the element of the collection with the maximum value.
    """
    return max(enumerate(collection), key=lambda x: x[1])[0]


# this controls the ordering of the summary message
SUMMARY_STATES = [
    JobStatus.COMPLETED,
    JobStatus.REMOVED,
    JobStatus.IDLE,
    JobStatus.RUNNING,
    JobStatus.HELD,
    JobStatus.SUSPENDED,
]
SUMMARY_MESSAGES = ["completed", "removed", "idle", "running", "held", "suspended"]


def make_summary_with_totals(totals, width=None):
    strip = 11

    while True:
        strip -= 1

        summary = "Total: {} jobs; {}".format(
            totals[TOTAL],
            ", ".join(
                "{} {}".format(totals[status], msg[:strip])
                for status, msg in zip(SUMMARY_STATES, SUMMARY_MESSAGES)
                if totals[status] > 0
            ),
        )

        if width is None or len(summary) <= width or strip <= 2:
            break

    return [summary]


def make_summary_with_percentages(totals, width=None):
    num_total = totals[TOTAL]
    percentages = [
        round(100.0 * safe_divide(totals[s], num_total)) for s in SUMMARY_STATES
    ]

    summary = "Total: {} jobs; {}".format(
        totals[TOTAL],
        ", ".join(
            "{}% {}".format(p, msg) for p, msg in zip(percentages, SUMMARY_MESSAGES)
        ),
    )

    if width is not None and len(summary[0]) > width:
        summary = "Total: {} jobs; {}".format(
            totals[TOTAL],
            ", ".join(
                "{}% {}".format(p, msg[0].upper())
                for p, msg in zip(percentages, SUMMARY_MESSAGES)
            ),
        )

    return [summary]


def safe_divide(numerator, denominator, default=0):
    if not numerator or not denominator:
        return default
    try:
        return numerator / denominator
    except ZeroDivisionError:
        return default


def warning(msg):
    print(fmt_warning(msg), file=sys.stderr)


def fmt_warning(msg):
    return "WARNING: {}".format(msg)


def error(msg):
    print(fmt_error(msg), file=sys.stderr)


def fmt_error(msg):
    return "ERROR: {}".format(msg)


if __name__ == "__main__":
    cli()
