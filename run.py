#!/usr/bin/env python3
"""Rerun WPT WebDriver BiDi test files against Safari stable through safaridriver.

Usage: python3 run.py [--dry-run] [--files files.txt] [--webkit PATH]

Each test file gets its own pytest process and a fresh safaridriver. Results go to
results-<timestamp>/ next to this script. See README.md.
"""
import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.dont_write_bytecode = True
import compare  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, "work")
VENV = os.path.join(HERE, "venv")
VENV_PYTHON = os.path.join(VENV, "bin", "python3")
W3C = os.path.join(WORK, "w3c")
DRIVER = "/System/Cryptexes/App/usr/bin/safaridriver"
DEPS = os.path.join(HERE, "deps")
WEBKIT_DEP = os.path.join(DEPS, "webkit")
WPT_DEP = os.path.join(DEPS, "web-platform-tests")
WEBKIT_REPO = "https://github.com/WebKit/WebKit.git"
IMPORT_DIR = "WebDriverTests/imported/w3c"
# Only the files WebKit adds on top of upstream WPT (its importer pin and the wptserve and pytest config).
WEBKIT_SPARSE = [f"/{IMPORT_DIR}/{name}" for name in ("importer.json", "config.json", "pytest.ini")]
WPT_SPARSE = ["/*", "/webdriver/", "/tools/", "/resources/", "/docs/"]  # root files include wpt.py and the wpt script it execs; docs/commands.json is read too
SAFARI_AUTOMATION = "Safari.app/Contents/MacOS/Safari .*--automation"
REQUIREMENTS = ["pytest==8.4.2", "pytest-asyncio==0.19.0", "pytest-timeout==2.4.0", "websockets==12.0"]
TEST_TIMEOUT = 25
FILE_CAP = 900

driver = None
wptserve = None


def log(message):
    print(message, flush=True)


def port_open(port):
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def driver_request(base, method, path):
    request = urllib.request.Request(base + path, method=method)
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)["value"]


def safari_automation_pids():
    out = subprocess.run(["pgrep", "-f", SAFARI_AUTOMATION], capture_output=True, text=True).stdout
    return {int(pid) for pid in out.split()}


def stop_driver():
    global driver
    if driver is not None:
        driver.terminate()
        try:
            driver.wait(timeout=5)
        except subprocess.TimeoutExpired:
            driver.kill()
            driver.wait()
        driver = None


def start_driver(port, log_path):
    global driver
    stop_driver()
    if port_open(port):
        sys.exit(f"port {port} is already in use; stop the other safaridriver first")
    driver = subprocess.Popen([DRIVER, "-p", str(port)], stdout=open(log_path, "a"), stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        if driver.poll() is not None:
            break
        try:
            if driver_request(base, "GET", "/status").get("ready"):
                return
        except OSError:
            pass
        time.sleep(0.2)
    sys.exit("safaridriver did not become ready")


def delete_sessions(port, id_log):
    """Delete the sessions the last pytest process created, in case it left one open."""
    if not os.path.exists(id_log):
        return
    with open(id_log) as f:
        ids = [line.strip() for line in f if line.strip()]
    os.remove(id_log)
    for session_id in ids:
        try:
            driver_request(f"http://127.0.0.1:{port}", "DELETE", f"/session/{session_id}")
        except Exception:
            pass  # already ended


def stop_wptserve():
    global wptserve
    if wptserve is None:
        return
    # wptserve forks one process per server, so signal the whole group.
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(wptserve.pid, sig)
        except (ProcessLookupError, PermissionError):
            break  # group is gone (macOS reports EPERM once only a zombie is left)
        try:
            wptserve.wait(timeout=5)
        except subprocess.TimeoutExpired:
            continue
        time.sleep(1)  # let the forked servers exit before checking the group again
    wptserve.wait()
    wptserve = None


def start_wptserve(wpt_root, ports):
    global wptserve
    for port in ports:
        if port_open(port):
            sys.exit(f"port {port} is already in use; stop the other wptserve first")
    with open(os.path.join(W3C, "config.json")) as f:
        config = json.load(f)
    config["doc_root"] = W3C
    config["ssl"]["openssl"]["base_path"] = os.path.join(WORK, "certs")
    # Homebrew OpenSSL 3.5 fails to generate the CA with wptserve's config; LibreSSL works.
    config["ssl"]["openssl"]["openssl_binary"] = "/usr/bin/openssl"
    config_path = os.path.join(WORK, "serve-config.json")
    with open(config_path, "w") as f:
        json.dump(config, f, indent=1)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    wptserve = subprocess.Popen(
        [VENV_PYTHON, "wpt.py", "--venv", VENV, "--skip-venv-setup", "serve", "--config", config_path],
        cwd=wpt_root, env=env, stdout=open(os.path.join(WORK, "wptserve.log"), "w"), stderr=subprocess.STDOUT,
        start_new_session=True)
    deadline = time.time() + 60
    while time.time() < deadline:
        if wptserve.poll() is not None:
            sys.exit(f"wptserve exited early, see {WORK}/wptserve.log")
        if all(port_open(port) for port in ports):
            return
        time.sleep(0.5)
    sys.exit(f"wptserve did not start, see {WORK}/wptserve.log")


def check_remote_automation(port, log_path):
    """Fail early unless Safari allows WebDriver control (`safaridriver --enable`)."""
    start_driver(port, log_path)
    base = f"http://127.0.0.1:{port}"
    body = json.dumps({"capabilities": {"alwaysMatch": {"browserName": "safari"}}}).encode()
    request = urllib.request.Request(base + "/session", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            session_id = json.load(response)["value"]["sessionId"]
    except urllib.error.HTTPError as error:
        message = json.load(error)["value"].get("message", "")
        if "remote automation" in message:
            sys.exit("Safari does not allow WebDriver control yet. Run `safaridriver --enable` in Terminal "
                     "(it asks for your admin password), then run this again.")
        sys.exit(f"could not create a Safari session: {message}")
    try:
        urllib.request.urlopen(urllib.request.Request(f"{base}/session/{session_id}", method="DELETE"), timeout=30)
    finally:
        stop_driver()


def git(path, *args):
    result = subprocess.run(["git", *args], cwd=path, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed in {path}:\n{result.stderr.strip()}")
    return result.stdout.strip()


def sparse_fetch(path, url, ref, sparse_paths, cone):
    """Shallow, sparse checkout of one ref into path. Re-running updates the checkout."""
    if not os.path.isdir(os.path.join(path, ".git")):
        os.makedirs(path, exist_ok=True)
        git(path, "init", "-q")
        git(path, "remote", "add", "origin", url)
    git(path, "sparse-checkout", "init", "--cone" if cone else "--no-cone")
    git(path, "sparse-checkout", "set", *sparse_paths)
    git(path, "fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", ref)
    git(path, "checkout", "-q", "--detach", "FETCH_HEAD")
    return git(path, "rev-parse", "HEAD")


def resolve_sources(webkit):
    """Return where the tests, tools, and wptserve come from, and how to describe them in results."""
    if webkit:
        root = os.path.abspath(os.path.expanduser(webkit))
        w3c = os.path.join(root, *IMPORT_DIR.split("/"))
        if not os.path.isdir(w3c):
            sys.exit(f"WPT import not found: {w3c}")
        log(f"Using the WebKit checkout at {root} (--webkit): tests, tools and wptserve come from it.")
        return {
            "copies": [(w3c, "")],
            "wpt_root": os.path.join(root, "LayoutTests", "imported", "w3c", "web-platform-tests"),
            "provenance": {"mode": "webkit-checkout", "webkit": root},
        }

    log("Step 1/3: reading the WPT revision WebKit pins. Sparse shallow clone of WebKit, "
        f"only {', '.join(WEBKIT_SPARSE)}, into {os.path.relpath(WEBKIT_DEP, HERE)}/.")
    webkit_commit = sparse_fetch(WEBKIT_DEP, WEBKIT_REPO, "main", WEBKIT_SPARSE, cone=False)
    with open(os.path.join(WEBKIT_DEP, IMPORT_DIR, "importer.json")) as f:
        pin = json.load(f)
    wpt_repo, revision = pin["repository"], pin["revision"]
    log(f"  WebKit main {webkit_commit[:12]} pins WPT {revision[:12]}")

    log(f"Step 2/3: fetching WPT {revision[:12]}. Shallow sparse clone of only "
        f"{', '.join(WPT_SPARSE)} into {os.path.relpath(WPT_DEP, HERE)}/.")
    wpt_commit = sparse_fetch(WPT_DEP, wpt_repo, revision, WPT_SPARSE, cone=False)
    if wpt_commit != revision:
        sys.exit(f"fetched WPT {wpt_commit}, expected {revision}")

    log("Step 3/3: staging the tests from WPT, plus WebKit's config.json and pytest.ini.")
    return {
        "copies": [
            (os.path.join(WPT_DEP, "webdriver"), "webdriver"),
            (os.path.join(WPT_DEP, "tools"), "tools"),
            (os.path.join(WPT_DEP, "resources"), "resources"),
            (os.path.join(WEBKIT_DEP, IMPORT_DIR, "config.json"), "config.json"),
            (os.path.join(WEBKIT_DEP, IMPORT_DIR, "pytest.ini"), "pytest.ini"),
        ],
        "wpt_root": WPT_DEP,
        "provenance": {
            "mode": "shallow-clone",
            "webkit_commit": webkit_commit,
            "wpt_revision": revision,
        },
    }


def stage_tests(sources):
    """Copy the test tree into a scratch directory so pytest never writes into a checkout."""
    if os.path.exists(W3C):
        shutil.rmtree(W3C)
    os.makedirs(W3C)
    ignore = shutil.ignore_patterns("__pycache__")
    for src, dst in sources["copies"]:
        target = os.path.join(W3C, dst)
        if os.path.isdir(src):
            shutil.copytree(src, target, ignore=ignore, dirs_exist_ok=True)
        else:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(src, target)


def prepare(sources):
    if not os.path.exists(DRIVER):
        sys.exit(f"safaridriver not found: {DRIVER}")
    os.makedirs(WORK, exist_ok=True)
    stage_tests(sources)
    if not os.path.exists(VENV_PYTHON):
        log("creating venv")
        subprocess.run([sys.executable, "-m", "venv", VENV], check=True)
        subprocess.run([VENV_PYTHON, "-m", "pip", "install", "-q", "--disable-pip-version-check", *REQUIREMENTS], check=True)
    with open(os.path.join(HERE, "wdspec.json")) as f:
        spec = json.load(f)
    spec["wptserve"]["doc_root"] = W3C
    spec_path = os.path.join(WORK, "wdspec.json")
    with open(spec_path, "w") as f:
        json.dump(spec, f, indent=1)
    return spec, spec_path


def read_files(path):
    with open(path) as f:
        files = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    missing = [f for f in files if not os.path.isfile(os.path.join(W3C, "webdriver", f))]
    if missing:
        sys.exit("test files not found:\n  " + "\n  ".join(missing))
    return files


def run_files(files, spec, spec_path, results):
    port = spec["port"]
    id_log = os.path.join(results, "session-ids.tmp")
    env = dict(os.environ,
               WDSPEC_CONFIG_FILE=spec_path,
               PYTHONPATH=os.pathsep.join([os.path.join(W3C, "tools", "webdriver"), os.path.join(HERE, "plugins")]),
               PYTHONDONTWRITEBYTECODE="1",
               SESSION_ERR_LOG=os.path.join(results, "session-errors.log"),
               SESSION_ID_LOG=id_log)
    totals = [0] * 4
    for i, test_file in enumerate(files):
        start_driver(port, os.path.join(results, "driver.log"))  # fresh driver per file, so one file cannot break the next
        name = test_file.replace("/", "__")
        xml = os.path.join(results, name + ".xml")
        command = [VENV_PYTHON, "-m", "pytest", "--rootdir", W3C, "--strict-markers",
                   "-p", "no:cacheprovider", "-p", "sessionlog", "--basetemp", os.path.join(WORK, "pytest-tmp"),
                   "-o", "console_output_style=classic", "--timeout", str(TEST_TIMEOUT), "-q", "-rfEs",
                   "--junitxml", xml, test_file]
        started = time.time()
        note = ""
        with open(os.path.join(results, name + ".log"), "w") as output:
            try:
                subprocess.run(command, cwd=os.path.join(W3C, "webdriver"), env=env, stdout=output,
                               stderr=subprocess.STDOUT, timeout=FILE_CAP)
            except subprocess.TimeoutExpired:
                note = f" KILLED after {FILE_CAP}s, remaining tests not run"
        delete_sessions(port, id_log)
        counts = compare.counts(compare.load_file(xml)) if os.path.exists(xml) else [0] * 4
        totals = [a + b for a, b in zip(totals, counts)]
        log(f"{i + 1}/{len(files)} {compare.format_counts(counts)} {time.time() - started:.0f}s {test_file}{note}")
    log(f"\ntotal: {compare.format_counts(totals)} ({sum(totals)} tests, {len(files)} files)")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--dry-run", action="store_true", help="check the setup and start/stop the servers without running tests")
    parser.add_argument("--files", default=os.path.join(HERE, "files.txt"), help="list of test files (default: files.txt)")
    parser.add_argument("--webkit", metavar="PATH", help="use this WebKit checkout (only read) instead of fetching WPT")
    args = parser.parse_args()

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGHUP, interrupt)

    safari_before = safari_automation_pids()
    status = 0
    try:
        sources = resolve_sources(args.webkit)
        spec, spec_path = prepare(sources)
        files = read_files(args.files)
        check_remote_automation(spec["port"], os.path.join(WORK, "preflight-driver.log"))
        log("remote automation: enabled")
        ports = [spec["wptserve"]["ports"]["http"][0], spec["wptserve"]["ports"]["https"][0]]
        start_wptserve(sources["wpt_root"], ports)
        log(f"wptserve listening on ports {ports}")
        if args.dry_run:
            url = f"http://localhost:{ports[0]}/webdriver/tests/support/inline.py?doc=ok"
            with urllib.request.urlopen(url, timeout=10) as response:
                log(f"inline handler: HTTP {response.status}")
            start_driver(spec["port"], os.path.join(WORK, "driver-dry-run.log"))
            log(f"safaridriver ready on port {spec['port']} (no session created)")
            log(f"{len(files)} test files:")
            for test_file in files:
                log(f"  {test_file}")
            log(f"config: {spec_path}\nscratch copy: {W3C}")
        else:
            results = os.path.join(HERE, "results-" + time.strftime("%Y%m%d-%H%M%S"))
            os.makedirs(results)
            with open(os.path.join(results, "sources.json"), "w") as f:
                json.dump(sources["provenance"], f, indent=2)
            log(f"results: {results}")
            run_files(files, spec, spec_path, results)
            log(f"\ncompare with: python3 compare.py {os.path.basename(results)}")
    except KeyboardInterrupt:
        log("\ninterrupted, cleaning up")
        status = 130
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        stop_driver()
        stop_wptserve()
        # safaridriver can leave its automation Safari running; stop only the ones started during this run.
        for pid in safari_automation_pids() - safari_before:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        shutil.rmtree(os.path.join(WORK, "pytest-tmp"), ignore_errors=True)
    sys.exit(status)


if __name__ == "__main__":
    main()
