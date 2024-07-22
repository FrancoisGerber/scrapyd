import datetime
import io
import os
import re
import sys
from unittest.mock import MagicMock, call

import pytest
from twisted.web import error

from scrapyd.exceptions import DirectoryTraversalError, RunnerError
from scrapyd.interfaces import IEggStorage
from scrapyd.jobstorage import Job
from scrapyd.launcher import ScrapyProcessProtocol
from scrapyd.txapp import application
from scrapyd.webservice import spider_list
from tests import get_egg_data, has_settings, root_add_version

job1 = Job(
    project="p1",
    spider="s1",
    job="j1",
    start_time=datetime.datetime(2001, 2, 3, 4, 5, 6, 7),
    end_time=datetime.datetime(2001, 2, 3, 4, 5, 6, 8),
)


@pytest.fixture()
def app(chdir):
    return application


@pytest.fixture()
def scrapy_process():
    process = ScrapyProcessProtocol(project="p1", spider="s1", job="j1", env={}, args=[])
    process.start_time = datetime.datetime(2001, 2, 3, 4, 5, 6, 9)
    process.transport = MagicMock()
    return process


def get_local_projects(root):
    return ["localproject"] if has_settings(root) else []


def add_test_version(app, project, version, basename):
    app.getComponent(IEggStorage).put(io.BytesIO(get_egg_data(basename)), project, version)


def assert_content(txrequest, root, method, basename, args, expected):
    txrequest.args = args.copy()
    content = getattr(root.children[b"%b.json" % basename.encode()], f"render_{method}")(txrequest)

    assert content.pop("node_name")
    assert content == {"status": "ok", **expected}


def assert_error(txrequest, root, method, basename, args, message):
    txrequest.args = args.copy()
    with pytest.raises(error.Error) as exc:
        getattr(root.children[b"%b.json" % basename.encode()], f"render_{method}")(txrequest)

    assert exc.value.status == b"200"
    assert exc.value.message == message


def test_spider_list(app):
    add_test_version(app, "myproject", "r1", "mybot")
    spiders = spider_list.get("myproject", None, runner="scrapyd.runner")
    assert sorted(spiders) == ["spider1", "spider2"]

    # Use the cache.
    add_test_version(app, "myproject", "r2", "mybot2")
    spiders = spider_list.get("myproject", None, runner="scrapyd.runner")
    assert sorted(spiders) == ["spider1", "spider2"]  # mybot2 has 3 spiders, but the cache wasn't evicted

    # Clear the cache.
    spider_list.delete("myproject")
    spiders = spider_list.get("myproject", None, runner="scrapyd.runner")
    assert sorted(spiders) == ["spider1", "spider2", "spider3"]

    # Re-add the 2-spider version and clear the cache.
    add_test_version(app, "myproject", "r3", "mybot")
    spider_list.delete("myproject")
    spiders = spider_list.get("myproject", None, runner="scrapyd.runner")
    assert sorted(spiders) == ["spider1", "spider2"]

    # Re-add the 3-spider version and clear the cache, but use a lower version number.
    add_test_version(app, "myproject", "r1a", "mybot2")
    spider_list.delete("myproject")
    spiders = spider_list.get("myproject", None, runner="scrapyd.runner")
    assert sorted(spiders) == ["spider1", "spider2"]


def test_spider_list_log_stdout(app):
    add_test_version(app, "logstdout", "logstdout", "logstdout")
    spiders = spider_list.get("logstdout", None, runner="scrapyd.runner")

    assert sorted(spiders) == ["spider1", "spider2"]  # [] if LOG_STDOUT were enabled


def test_spider_list_unicode(app):
    add_test_version(app, "myprojectunicode", "r1", "mybotunicode")
    spiders = spider_list.get("myprojectunicode", None, runner="scrapyd.runner")

    assert sorted(spiders) == ["araña1", "araña2"]


def test_spider_list_error(app):
    # mybot3.settings contains "raise Exception('This should break the `scrapy list` command')".
    add_test_version(app, "myproject3", "r1", "mybot3")
    with pytest.raises(RunnerError) as exc:
        spider_list.get("myproject3", None, runner="scrapyd.runner")

    assert re.search(f"Exception: This should break the `scrapy list` command{os.linesep}$", str(exc.value))


@pytest.mark.parametrize(
    ("method", "basename", "param", "args"),
    [
        ("POST", "schedule", "project", {}),
        ("POST", "schedule", "project", {b"spider": [b"scrapy-css"]}),
        ("POST", "schedule", "spider", {b"project": [b"quotesbot"]}),
        ("POST", "cancel", "project", {}),
        ("POST", "cancel", "project", {b"job": [b"aaa"]}),
        ("POST", "cancel", "job", {b"project": [b"quotesbot"]}),
        ("POST", "addversion", "project", {}),
        ("POST", "addversion", "project", {b"version": [b"0.1"]}),
        ("POST", "addversion", "version", {b"project": [b"quotesbot"]}),
        ("GET", "listversions", "project", {}),
        ("GET", "listspiders", "project", {}),
        ("GET", "status", "job", {}),
        ("POST", "delproject", "project", {}),
        ("POST", "delversion", "project", {}),
        ("POST", "delversion", "project", {b"version": [b"0.1"]}),
        ("POST", "delversion", "version", {b"project": [b"quotesbot"]}),
    ],
)
def test_required(txrequest, root_with_egg, method, basename, param, args):
    message = b"'%b' parameter is required" % param.encode()
    assert_error(txrequest, root_with_egg, method, basename, args, message)


def test_invalid_utf8(txrequest, root):
    args = {b"project": [b"\xc3\x28"]}
    message = b"project is invalid: 'utf-8' codec can't decode byte 0xc3 in position 0: invalid continuation byte"
    assert_error(txrequest, root, "GET", "listversions", args, message)


def test_invalid_type(txrequest, root):
    args = {b"project": [b"p"], b"spider": [b"s"], b"priority": [b"x"]}
    message = b"priority is invalid: could not convert string to float: b'x'"
    assert_error(txrequest, root, "POST", "schedule", args, message)


def test_daemonstatus(txrequest, root_with_egg, scrapy_process):
    expected = {"running": 0, "pending": 0, "finished": 0}
    assert_content(txrequest, root_with_egg, "GET", "daemonstatus", {}, expected)

    root_with_egg.launcher.finished.add(job1)
    expected["finished"] += 1
    assert_content(txrequest, root_with_egg, "GET", "daemonstatus", {}, expected)

    root_with_egg.launcher.processes[0] = scrapy_process
    expected["running"] += 1
    assert_content(txrequest, root_with_egg, "GET", "daemonstatus", {}, expected)

    root_with_egg.scheduler.queues["quotesbot"].add("quotesbot")
    expected["pending"] += 1
    assert_content(txrequest, root_with_egg, "GET", "daemonstatus", {}, expected)


@pytest.mark.parametrize(
    ("args", "spiders", "run_only_if_has_settings"),
    [
        ({b"project": [b"myproject"]}, ["spider1", "spider2", "spider3"], False),
        ({b"project": [b"myproject"], b"_version": [b"r1"]}, ["spider1", "spider2"], False),
        ({b"project": [b"localproject"]}, ["example"], True),
    ],
)
def test_list_spiders(txrequest, root, args, spiders, run_only_if_has_settings):
    if run_only_if_has_settings and not has_settings(root):
        pytest.skip("[settings] section is not set")

    root_add_version(root, "myproject", "r1", "mybot")
    root_add_version(root, "myproject", "r2", "mybot2")
    root.update_projects()

    expected = {"spiders": spiders}
    assert_content(txrequest, root, "GET", "listspiders", args, expected)


@pytest.mark.parametrize(
    ("args", "param", "run_only_if_has_settings"),
    [
        ({b"project": [b"nonexistent"]}, "project", False),
        ({b"project": [b"myproject"], b"_version": [b"nonexistent"]}, "version", False),
        ({b"project": [b"localproject"], b"_version": [b"nonexistent"]}, "version", True),
    ],
)
def test_list_spiders_nonexistent(txrequest, root, args, param, run_only_if_has_settings):
    if run_only_if_has_settings and not has_settings(root):
        pytest.skip("[settings] section is not set")

    root_add_version(root, "myproject", "r1", "mybot")
    root_add_version(root, "myproject", "r2", "mybot2")
    root.update_projects()

    assert_error(txrequest, root, "GET", "listspiders", args, b"%b 'nonexistent' not found" % param.encode())


def test_list_versions(txrequest, root_with_egg):
    expected = {"versions": ["0_1"]}
    assert_content(txrequest, root_with_egg, "GET", "listversions", {b"project": [b"quotesbot"]}, expected)


def test_list_versions_nonexistent(txrequest, root):
    expected = {"versions": []}
    assert_content(txrequest, root, "GET", "listversions", {b"project": [b"localproject"]}, expected)


def test_list_projects(txrequest, root_with_egg):
    expected = {"projects": ["quotesbot", *get_local_projects(root_with_egg)]}
    assert_content(txrequest, root_with_egg, "GET", "listprojects", {}, expected)


def test_list_projects_empty(txrequest, root):
    expected = {"projects": get_local_projects(root)}
    assert_content(txrequest, root, "GET", "listprojects", {}, expected)


@pytest.mark.parametrize("args", [{}, {b"project": [b"p1"]}])
def test_status(txrequest, root, scrapy_process, args):
    root_add_version(root, "p1", "r1", "mybot")
    root_add_version(root, "p2", "r2", "mybot2")
    root.update_projects()

    if args:
        root.launcher.finished.add(Job(project="p2", spider="s2", job="j1"))
        root.launcher.processes[0] = ScrapyProcessProtocol("p2", "s2", "j1", {}, [])
        root.scheduler.queues["p2"].add("s2", _job="j1")

    expected = {"currstate": None}
    assert_content(txrequest, root, "GET", "status", {b"job": [b"j1"], **args}, expected)

    root.scheduler.queues["p1"].add("s1", _job="j1")

    expected["currstate"] = "pending"
    assert_content(txrequest, root, "GET", "status", {b"job": [b"j1"], **args}, expected)

    root.launcher.processes[0] = scrapy_process

    expected["currstate"] = "running"
    assert_content(txrequest, root, "GET", "status", {b"job": [b"j1"], **args}, expected)

    root.launcher.finished.add(job1)

    expected["currstate"] = "finished"
    assert_content(txrequest, root, "GET", "status", {b"job": [b"j1"], **args}, expected)


def test_status_nonexistent(txrequest, root):
    args = {b"job": [b"aaa"], b"project": [b"nonexistent"]}
    assert_error(txrequest, root, "GET", "status", args, b"project 'nonexistent' not found")


@pytest.mark.parametrize("args", [{}, {b"project": [b"p1"]}])
def test_list_jobs(txrequest, root, scrapy_process, args):
    root_add_version(root, "p1", "r1", "mybot")
    root_add_version(root, "p2", "r2", "mybot2")
    root.update_projects()

    if args:
        root.launcher.finished.add(Job(project="p2", spider="s2", job="j2"))
        root.launcher.processes[0] = ScrapyProcessProtocol("p2", "s2", "j2", {}, [])
        root.scheduler.queues["p2"].add("s2", _job="j2")

    expected = {"pending": [], "running": [], "finished": []}
    assert_content(txrequest, root, "GET", "listjobs", args, expected)

    root.launcher.finished.add(job1)

    expected["finished"].append(
        {
            "id": "j1",
            "project": "p1",
            "spider": "s1",
            "start_time": "2001-02-03 04:05:06.000007",
            "end_time": "2001-02-03 04:05:06.000008",
            "items_url": "/items/p1/s1/j1.jl",
            "log_url": "/logs/p1/s1/j1.log",
        },
    )
    assert_content(txrequest, root, "GET", "listjobs", args, expected)

    root.launcher.processes[0] = scrapy_process

    expected["running"].append(
        {
            "id": "j1",
            "project": "p1",
            "spider": "s1",
            "start_time": "2001-02-03 04:05:06.000009",
            "pid": None,
        }
    )
    assert_content(txrequest, root, "GET", "listjobs", args, expected)

    root.scheduler.queues["p1"].add("s1", _job="j1")

    expected["pending"].append(
        {
            "id": "j1",
            "project": "p1",
            "spider": "s1",
        },
    )
    assert_content(txrequest, root, "GET", "listjobs", args, expected)


def test_list_jobs_nonexistent(txrequest, root):
    args = {b"project": [b"nonexistent"]}
    assert_error(txrequest, root, "GET", "listjobs", args, b"project 'nonexistent' not found")


def test_delete_version(txrequest, root):
    projects = get_local_projects(root)

    root_add_version(root, "myproject", "r1", "mybot")
    root_add_version(root, "myproject", "r2", "mybot2")
    root.update_projects()

    # Spiders (before).
    expected = {"spiders": ["spider1", "spider2", "spider3"]}
    assert_content(txrequest, root, "GET", "listspiders", {b"project": [b"myproject"]}, expected)

    # Delete one version.
    args = {b"project": [b"myproject"], b"version": [b"r2"]}
    assert_content(txrequest, root, "POST", "delversion", args, {"status": "ok"})
    assert root.eggstorage.get("myproject", "r2") == (None, None)  # version is gone

    # Spiders (after) would contain "spider3" without cache eviction.
    expected = {"spiders": ["spider1", "spider2"]}
    assert_content(txrequest, root, "GET", "listspiders", {b"project": [b"myproject"]}, expected)

    # Projects (before).
    assert_content(txrequest, root, "GET", "listprojects", {}, {"projects": ["myproject", *projects]})

    # Delete another version.
    args = {b"project": [b"myproject"], b"version": [b"r1"]}
    assert_content(txrequest, root, "POST", "delversion", args, {"status": "ok"})
    assert root.eggstorage.get("myproject") == (None, None)  # project is gone

    # Projects (after) would contain "myproject" without root.update_projects().
    assert_content(txrequest, root, "GET", "listprojects", {}, {"projects": [*projects]})


def test_delete_version_uncached(txrequest, root_with_egg):
    args = {b"project": [b"quotesbot"], b"version": [b"0.1"]}
    assert_content(txrequest, root_with_egg, "POST", "delversion", args, {"status": "ok"})


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({b"project": [b"quotesbot"], b"version": [b"nonexistent"]}, b"version 'nonexistent' not found"),
        ({b"project": [b"nonexistent"], b"version": [b"0.1"]}, b"version '0.1' not found"),
    ],
)
def test_delete_version_nonexistent(txrequest, root_with_egg, args, message):
    assert_error(txrequest, root_with_egg, "POST", "delversion", args, message)


def test_delete_project(txrequest, root_with_egg):
    projects = get_local_projects(root_with_egg)

    # Spiders (before).
    expected = {"spiders": ["toscrape-css", "toscrape-xpath"]}
    assert_content(txrequest, root_with_egg, "GET", "listspiders", {b"project": [b"quotesbot"]}, expected)

    # Projects (before).
    expected = {"projects": ["quotesbot", *projects]}
    assert_content(txrequest, root_with_egg, "GET", "listprojects", {}, expected)

    # Delete the project.
    args = {b"project": [b"quotesbot"]}
    assert_content(txrequest, root_with_egg, "POST", "delproject", args, {"status": "ok"})
    assert root_with_egg.eggstorage.get("quotesbot") == (None, None)  # project is gone

    # Spiders (after).
    args = {b"project": [b"quotesbot"]}
    assert_error(txrequest, root_with_egg, "GET", "listspiders", args, b"project 'quotesbot' not found")

    # Projects (after) would contain "quotesbot" without root.update_projects().
    expected = {"projects": [*projects]}
    assert_content(txrequest, root_with_egg, "GET", "listprojects", {}, expected)


def test_delete_project_uncached(txrequest, root_with_egg):
    args = {b"project": [b"quotesbot"]}
    assert_content(txrequest, root_with_egg, "POST", "delproject", args, {"status": "ok"})


def test_delete_project_nonexistent(txrequest, root):
    args = {b"project": [b"nonexistent"]}
    assert_error(txrequest, root, "POST", "delproject", args, b"project 'nonexistent' not found")


def test_add_version(txrequest, root):
    assert root.eggstorage.get("quotesbot") == (None, None)

    # Add a version.
    args = {b"project": [b"quotesbot"], b"version": [b"0.1"], b"egg": [get_egg_data("quotesbot")]}
    expected = {"project": "quotesbot", "version": "0.1", "spiders": 2}
    assert_content(txrequest, root, "POST", "addversion", args, expected)
    assert root.eggstorage.list("quotesbot") == ["0_1"]

    # Spiders (before).
    expected = {"spiders": ["toscrape-css", "toscrape-xpath"]}
    assert_content(txrequest, root, "GET", "listspiders", {b"project": [b"quotesbot"]}, expected)

    # Add the same version with a different egg.
    args = {b"project": [b"quotesbot"], b"version": [b"0.1"], b"egg": [get_egg_data("mybot2")]}
    expected = {"project": "quotesbot", "version": "0.1", "spiders": 3}  # 2 without cache eviction
    assert_content(txrequest, root, "POST", "addversion", args, expected)
    assert root.eggstorage.list("quotesbot") == ["0_1"]  # overwrite version

    # Spiders (after).
    expected = {"spiders": ["spider1", "spider2", "spider3"]}
    assert_content(txrequest, root, "GET", "listspiders", {b"project": [b"quotesbot"]}, expected)


def test_add_version_settings(txrequest, root):
    if not has_settings(root):
        pytest.skip("[settings] section is not set")

    args = {b"project": [b"localproject"], b"version": [b"0.1"], b"egg": [get_egg_data("quotesbot")]}
    expected = {"project": "localproject", "spiders": 2, "version": "0.1"}
    assert_content(txrequest, root, "POST", "addversion", args, expected)


def test_add_version_invalid(txrequest, root):
    args = {b"project": [b"quotesbot"], b"version": [b"0.1"], b"egg": [b"invalid"]}
    message = b"egg is not a ZIP file (if using curl, use egg=@path not egg=path)"
    assert_error(txrequest, root, "POST", "addversion", args, message)


def test_schedule(txrequest, root_with_egg):
    assert root_with_egg.scheduler.queues["quotesbot"].list() == []

    txrequest.args = {b"project": [b"quotesbot"], b"spider": [b"toscrape-css"]}
    content = root_with_egg.children[b"schedule.json"].render_POST(txrequest)
    jobs = root_with_egg.scheduler.queues["quotesbot"].list()
    jobs[0].pop("_job")

    assert len(jobs) == 1
    assert jobs[0] == {"name": "toscrape-css", "settings": {}, "version": None}
    assert content["status"] == "ok"
    assert "jobid" in content


def test_schedule_nonexistent_project(txrequest, root):
    args = {b"project": [b"nonexistent"], b"spider": [b"toscrape-css"]}
    assert_error(txrequest, root, "POST", "schedule", args, b"project 'nonexistent' not found")


def test_schedule_nonexistent_version(txrequest, root_with_egg):
    args = {b"project": [b"quotesbot"], b"_version": [b"nonexistent"], b"spider": [b"toscrape-css"]}
    assert_error(txrequest, root_with_egg, "POST", "schedule", args, b"version 'nonexistent' not found")


def test_schedule_nonexistent_spider(txrequest, root_with_egg):
    args = {b"project": [b"quotesbot"], b"spider": [b"nonexistent"]}
    assert_error(txrequest, root_with_egg, "POST", "schedule", args, b"spider 'nonexistent' not found")


@pytest.mark.parametrize("args", [{}, {b"signal": [b"TERM"]}])
def test_cancel(txrequest, root, scrapy_process, args):
    signal = "TERM" if args else ("INT" if sys.platform != "win32" else "BREAK")

    root_add_version(root, "p1", "r1", "mybot")
    root_add_version(root, "p2", "r2", "mybot2")
    root.update_projects()

    args = {b"project": [b"p1"], b"job": [b"j1"], **args}

    expected = {"prevstate": None}
    assert_content(txrequest, root, "POST", "cancel", args, expected)

    root.scheduler.queues["p1"].add("s1", _job="j1")
    root.scheduler.queues["p1"].add("s1", _job="j1")
    root.scheduler.queues["p1"].add("s1", _job="j2")

    assert root.scheduler.queues["p1"].count() == 3
    expected["prevstate"] = "pending"
    assert_content(txrequest, root, "POST", "cancel", args, expected)
    assert root.scheduler.queues["p1"].count() == 1

    root.launcher.processes[0] = scrapy_process
    root.launcher.processes[1] = scrapy_process
    root.launcher.processes[2] = ScrapyProcessProtocol("p2", "s2", "j2", {}, [])

    expected["prevstate"] = "running"
    assert_content(txrequest, root, "POST", "cancel", args, expected)
    assert scrapy_process.transport.signalProcess.call_count == 2
    scrapy_process.transport.signalProcess.assert_has_calls([call(signal), call(signal)])


def test_cancel_nonexistent(txrequest, root):
    args = {b"project": [b"nonexistent"], b"job": [b"aaa"]}
    assert_error(txrequest, root, "POST", "cancel", args, b"project 'nonexistent' not found")


# Cancel, Status, ListJobs and ListSpiders error with "project '%b' not found" on directory traversal attempts.
# The egg storage (in get_project_list, called by get_spider_queues, called by QueuePoller, used by these webservices)
# would need to find a project like "../project" (which is impossible with the default eggstorage) to not error.
@pytest.mark.parametrize(
    ("method", "basename", "args"),
    [
        ("POST", "cancel", {b"project": [b"../p"], b"job": [b"aaa"]}),
        ("GET", "status", {b"project": [b"../p"], b"job": [b"aaa"]}),
        ("GET", "listspiders", {b"project": [b"../p"]}),
        ("GET", "listjobs", {b"project": [b"../p"]}),
    ],
)
def test_project_directory_traversal_notfound(txrequest, root, method, basename, args):
    assert_error(txrequest, root, method, basename, args, b"project '../p' not found")


@pytest.mark.parametrize(
    ("endpoint", "attach_egg", "method"),
    [
        (b"addversion.json", True, "render_POST"),
        (b"listversions.json", False, "render_GET"),
        (b"delproject.json", False, "render_POST"),
        (b"delversion.json", False, "render_POST"),
    ],
)
def test_project_directory_traversal(txrequest, root, endpoint, attach_egg, method):
    txrequest.args = {b"project": [b"../p"], b"version": [b"0.1"]}

    if attach_egg:
        txrequest.args[b"egg"] = [get_egg_data("quotesbot")]

    with pytest.raises(DirectoryTraversalError) as exc:
        getattr(root.children[endpoint], method)(txrequest)

    assert str(exc.value) == "../p"

    eggstorage = root.app.getComponent(IEggStorage)
    assert eggstorage.get("quotesbot") == (None, None)


@pytest.mark.parametrize(
    ("endpoint", "method"),
    [
        (b"schedule.json", "render_POST"),
    ],
)
def test_project_directory_traversal_runner(txrequest, root, endpoint, method):
    txrequest.args = {b"project": [b"../p"], b"spider": [b"s"]}

    with pytest.raises(DirectoryTraversalError) as exc:
        getattr(root.children[endpoint], method)(txrequest)

    assert str(exc.value) == "../p"
