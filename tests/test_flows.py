"""Cross-file taint analysis (flows.py): sources -> calls across files -> sinks, plus its scanner / re-check /
AI-verification integration. Rebuilt 2026-11-19 after the original file was lost; same test names as before."""
import unittest
from unittest.mock import patch

import flows


def grammars_ok() -> bool:
    """tree-sitter grammars installed and loadable (downloaded once on first use)."""
    if not flows.available():
        return False
    try:
        flows.ensure_grammars({"python", "javascript", "java"})
        return True
    except RuntimeError:
        return False


GRAMMARS = grammars_ok()
NEEDS_GRAMMARS = unittest.skipUnless(GRAMMARS, "tree-sitter grammars not installed "
                                               "(pip install -r tools/requirements-scanners.txt)")

# ------------------------------------------------------------------------------------------------ Python fixtures
PY = {
    "app.py": '''from flask import Flask, request
import db
import net

app = Flask(__name__)


@app.route("/user")
def user():
    name = request.args["name"]
    return db.find_user(name)


@app.route("/ping/<host>")
def ping(host):
    return net.run_ping(host)
''',
    "db.py": '''import sqlite3

conn = sqlite3.connect(":memory:")


def find_user(username):
    sql = build_query(username)
    return conn.execute(sql).fetchall()


def build_query(u):
    return "SELECT * FROM users WHERE name = '" + u + "'"


def find_by_id(user_id):
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchall()
''',
    "net.py": '''import subprocess


def run_ping(target):
    return subprocess.check_output("ping -c 1 " + target, shell=True)
''',
}

REDIRECT = {"app.py": '''from flask import Flask, request, redirect

app = Flask(__name__)


def next_url():
    return request.args.get("next")


@app.route("/after/<step>")
def after(step):
    target = next_url()
    return redirect(target)
'''}

SANITIZED = {
    "app.py": '''from flask import Flask, request
import db
import net
import shlex

app = Flask(__name__)


@app.route("/count")
def count():
    n = int(request.args["n"])
    return db.find_user(n)


@app.route("/by-id")
def by_id():
    return db.find_by_id(request.args["id"])


@app.route("/quoted")
def quoted():
    return net.run_ping(shlex.quote(request.args["h"]))
''',
    "db.py": PY["db.py"],
    "net.py": PY["net.py"],
}


def kinds(found):
    """{(kind, file, line)} for compact assertions."""
    return {(f.kind, f.file, f.line) for f in found}


@NEEDS_GRAMMARS
class PythonFlows(unittest.TestCase):
    def setUp(self):
        self.found = flows.analyse_files(PY)

    def test_cross_file_sql_through_a_helper_that_returns_tainted_sql(self):
        sql = [f for f in self.found if f.kind == "sql"]
        self.assertEqual([(f.file, f.line) for f in sql], [("db.py", 8)])        # conn.execute(sql)
        f = sql[0]
        self.assertEqual((f.cwe, f.severity, f.files_crossed), ("CWE-89", "high", 2))
        self.assertTrue(f.trace[0].startswith("app.py:10 request.args"))
        self.assertIn("db.find_user", " ".join(f.trace))
        self.assertTrue(f.trace[-1].startswith("db.py:8 find_user: SQL query"))

    def test_route_parameter_reaches_shell_in_another_file(self):
        cmd = next(f for f in self.found if f.kind == "command")
        self.assertEqual((cmd.file, cmd.line, cmd.cwe), ("net.py", 5, "CWE-78"))
        self.assertIn("(request parameter)", cmd.trace[0])                       # <host> from the route
        self.assertEqual(cmd.files_crossed, 2)

    def test_tainted_return_value_and_redirect(self):
        found = flows.analyse_files(REDIRECT)
        self.assertEqual(kinds(found), {("redirect", "app.py", 13)})
        f = found[0]
        self.assertEqual((f.cwe, f.severity), ("CWE-601", "medium"))
        self.assertIn("next_url(...) returns user input", f.trace[0])

    def test_sanitized_and_parameterized_paths_are_not_reported(self):
        # int() cast, bound SQL parameter, shlex.quote(): nothing reaches a sink in a dangerous form
        self.assertEqual(flows.analyse_files(SANITIZED), [])

    def test_stored_template_injection(self):
        files = {"app.py": '''from flask import Flask, request, render_template

app = Flask(__name__)


def save_post(title):
    with open("templates/post.html", "a") as fh:
        fh.write(title)


@app.route("/new", methods=["POST"])
def new():
    save_post(request.form["title"])
    return render_template("post.html")
'''}
        found = flows.analyse_files(files)
        self.assertEqual(kinds(found), {("stored-template", "app.py", 8)})
        self.assertEqual((found[0].cwe, found[0].severity), ("CWE-1336", "medium"))


# ------------------------------------------------------------------------------------------------ JS / Java
@NEEDS_GRAMMARS
class JavaScriptAndJavaFlows(unittest.TestCase):
    def setUp(self):
        self.express = {
            "routes.js": '''const express = require("express");
const users = require("./users");
const router = express.Router();

router.get("/user", (req, res) => {
  const name = req.query.name;
  users.findByName(name).then((rows) => res.json(rows));
});
module.exports = router;
''',
            "users.js": '''const { sequelize } = require("./db");

function findByName(name) {
  return sequelize.query("SELECT * FROM users WHERE name = '" + name + "'");
}
module.exports = { findByName };
''',
        }

    def test_express_handler_to_sequelize_in_another_file(self):
        found = flows.analyse_files(self.express)
        self.assertEqual(kinds(found), {("sql", "users.js", 4)})
        self.assertEqual(found[0].files_crossed, 2)
        self.assertTrue(found[0].trace[0].startswith("routes.js:6 req.query"))

    def test_nosql_where_is_high_and_operator_injection_medium(self):
        files = {"api.js": '''const mongoose = require("mongoose");
const User = mongoose.model("User");

app.get("/where", (req, res) => {
  User.find({ $where: "this.name == '" + req.query.name + "'" }).then((u) => res.json(u));
});

app.post("/login", (req, res) => {
  User.findOne({ username: req.body.username, password: req.body.password }).then((u) => res.json(u));
});

app.get("/fixed", (req, res) => {
  User.findOne({ username: "admin" }).then((u) => res.json(u));
});
'''}
        by_line = {f.line: f for f in flows.analyse_files(files)}
        self.assertEqual(sorted(by_line), [5, 9])                                 # line 13 is a literal: not reported
        self.assertEqual((by_line[5].kind, by_line[5].severity), ("nosql", "high"))     # $where string
        self.assertEqual((by_line[9].kind, by_line[9].severity), ("nosql", "medium"))   # {"$ne": …} via JSON body

    def test_ssrf_and_dom_xss(self):
        files = {
            "server.js": '''const axios = require("axios");

app.get("/proxy", async (req, res) => {
  const url = req.query.url;
  const r = await axios.get(url);
  res.send(r.data);
});
''',
            "client.js": '''function render() {
  const q = location.search;
  document.getElementById("out").innerHTML = q;
}
''',
        }
        found = flows.analyse_files(files)
        self.assertEqual(kinds(found), {("ssrf", "server.js", 5), ("xss", "client.js", 3)})
        self.assertEqual({f.cwe for f in found}, {"CWE-918", "CWE-79"})

    def test_no_false_positives_for_hash_update_or_sequelize_objects(self):
        files = {"hash.js": '''const crypto = require("crypto");
const { User } = require("./models");

app.post("/sig", (req, res) => {
  const h = crypto.createHash("sha256");
  h.update(req.body.payload);
  res.send(h.digest("hex"));
});

app.get("/seq", (req, res) => {
  User.findOne({ where: { name: req.query.name } }).then((u) => res.json(u));
});
'''}
        # h.update() is not a database call; Sequelize (no Mongo in the file) doesn't take operators from JSON
        self.assertEqual(flows.analyse_files(files), [])

    def test_request_param_to_jdbc_in_service(self):
        files = {
            "src/UserController.java": '''import javax.servlet.http.HttpServletRequest;

public class UserController {
    private final UserService service = new UserService();

    public String show(HttpServletRequest request) {
        String id = request.getParameter("id");
        return service.lookup(id);
    }
}
''',
            "src/UserService.java": '''import java.sql.Connection;
import java.sql.Statement;

public class UserService {
    private Connection conn;

    public String lookup(String userId) {
        try {
            Statement st = conn.createStatement();
            st.executeQuery("SELECT * FROM users WHERE id = '" + userId + "'");
        } catch (Exception e) {
            return null;
        }
        return "ok";
    }
}
''',
        }
        found = flows.analyse_files(files)
        self.assertEqual(kinds(found), {("sql", "src/UserService.java", 10)})
        self.assertEqual(found[0].files_crossed, 2)
        self.assertIn("request.getParameter(", found[0].trace[0])


# ------------------------------------------------------------------------------------------------ robustness
class Robustness(unittest.TestCase):
    @NEEDS_GRAMMARS
    def test_recursion_and_bad_files_do_not_break_analysis(self):
        files = {"a.py": "from flask import request\nimport b\n\ndef f(x):\n    return b.g(x)\n\n"
                         "def h():\n    f(request.args['q'])\n",
                 "b.py": "import a\nimport os\n\ndef g(y):\n    os.system(y)\n    return a.f(y)\n",
                 "broken.py": "def (:\n", "big.js": "x".encode() * (flows.MAX_FILE_BYTES + 1)}
        found = flows.analyse_files(files)                          # a.f <-> b.g recursion, unparsable + oversized files
        self.assertEqual(kinds(found), {("command", "b.py", 5)})
        self.assertEqual(found[0].files_crossed, 2)

    def test_sanitizer_stripping(self):
        self.assertEqual(flows._strip_sanitized("int(request.args['n']) + x"), "0 + x")
        self.assertEqual(flows._strip_sanitized("escape(fmt(a, (b))) + y"), "0 + y")           # nested parentheses
        self.assertEqual(flows._strip_sanitized("shlex.quote(h) + ' ' + secure_filename(f)"), "0 + ' ' + 0")
        self.assertEqual(flows._strip_sanitized("request.args['q']"), "request.args['q']")       # untouched
        self.assertIsNone(flows._mentions(flows._strip_sanitized("int(uid)"), {"uid"}))
        self.assertEqual(flows._mentions("query + uid_x + uid", {"uid"}), "uid")                 # whole names only
        self.assertIsNone(flows._mentions("self.uid", {"uid"}))                                  # attribute, not the var

    def test_missing_grammar_gives_an_actionable_error(self):
        if not flows.available():
            self.skipTest("tree_sitter_language_pack not installed")
        with patch("tree_sitter_language_pack.get_parser", side_effect=OSError("offline")):
            with self.assertRaises(RuntimeError) as ctx:
                flows.ensure_grammars({"python"})
        self.assertIn("downloaded once on first use", str(ctx.exception))
        self.assertIn("python: OSError", str(ctx.exception))


# ------------------------------------------------------------------------------------------------ scanners.py
@NEEDS_GRAMMARS
class ScannerIntegration(unittest.TestCase):
    def test_flow_runs_as_a_scanner_with_traces(self):
        import scanners
        report = scanners.run_scanners(PY, scanners=["flow"])
        info = report.tools["flow"]
        self.assertEqual(info["status"], "ok", info)
        self.assertEqual(info["count"], 2)
        self.assertIn("cross files", info.get("note", ""))
        by_rule = {f.rule_id: f for f in report.findings}
        self.assertEqual(set(by_rule), {"taint-sql", "taint-command"})
        sql = by_rule["taint-sql"]
        self.assertEqual((sql.tool, sql.category, sql.file, sql.line), ("flow", "security", "db.py", 8))
        self.assertTrue(sql.trace and sql.trace[0].startswith("app.py:10"))
        self.assertIn("crosses 2 files", sql.message)
        self.assertIn("parameterized", sql.fix_hint)
        self.assertTrue(sql.help_url.endswith("/89.html"))

    def test_recheck_reruns_flow_on_the_whole_project(self):
        import scanners
        before = scanners.run_scanners(PY, scanners=["flow"]).findings
        before_db = [f for f in before if f.file == "db.py"]
        fixed = PY["db.py"].replace(
            "sql = build_query(username)\n    return conn.execute(sql).fetchall()",
            'return conn.execute("SELECT * FROM users WHERE name = ?", (username,)).fetchall()')
        self.assertNotEqual(fixed, PY["db.py"])
        check = scanners.recheck_file("db.py", fixed, before_db, project_files=PY)
        self.assertEqual(check["status"], "ok", check)
        self.assertEqual(check["tools"]["flow"]["status"], "ok")
        self.assertTrue(check["passed"], check)
        self.assertEqual([f.rule_id for f in check["resolved"]], ["taint-sql"])

        # without the rest of the project the source (app.py) is gone, so the flow could not even be seen:
        # the re-check must use project_files, otherwise "resolved" would be meaningless
        still_bad = scanners.recheck_file("db.py", PY["db.py"], before_db, project_files=PY)
        self.assertFalse(still_bad["passed"])
        self.assertEqual([f.rule_id for f in still_bad["remaining"]], ["taint-sql"])

    def test_merge_keeps_the_trace_when_semgrep_wins(self):
        import scanners
        flow = next(f for f in scanners.flow_findings(flows.analyse_files(PY)) if f.rule_id == "taint-sql")
        semgrep = scanners._make("semgrep", "python.sqlalchemy.security.sql-injection", "high", "security",
                                 "SQL injection", "msg", "db.py", 8, cwe=("CWE-89",))
        merged = scanners.merge_cross_tool([flow, semgrep])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].tool, "semgrep")                 # same severity: Semgrep is preferred
        self.assertEqual(merged[0].also, ("flow",))
        self.assertEqual(merged[0].trace, flow.trace)               # but the data-flow path is kept


# ------------------------------------------------------------------------------------------------ AI verification
class FlowVerification(unittest.TestCase):
    def setUp(self):
        import scanners
        self.scanners = scanners
        mk = scanners._make
        base = dict(category="security", title="t", message="m", cwe=("CWE-89",))
        self.files = {"app.py": PY["app.py"], "db.py": PY["db.py"]}
        trace = ("app.py:10 request.args (user input)", "app.py:11 user → db.find_user(...)",
                 "db.py:8 find_user: SQL query")
        self.findings = [
            scanners.Finding(**{**mk("flow", "taint-sql", "high", path="db.py", line=8, **base).__dict__, "trace": trace}),
            scanners.Finding(**{**mk("flow", "taint-command", "medium", path="net.py", line=5, **base).__dict__,
                                "trace": ("app.py:15 ping(host) (request parameter)", "net.py:5 run_ping: shell")}),
            scanners.Finding(**{**mk("flow", "taint-path", "low", path="db.py", line=14, **base).__dict__,
                                "trace": ("app.py:10 x", "db.py:14 y")}),
            mk("semgrep", "no-trace", "high", path="db.py", line=8, **base),          # not a flow: never sent
        ]

    def test_verdicts(self):
        import file_optimizer as fo
        replies = iter(['{"verdict": "confirmed", "reason": "string concatenation into execute"}',
                        'Sure! {"verdict": "FALSE_POSITIVE", "reason": "host is validated"}',
                        "not json at all"])
        prompts = []

        def fake(role, prompt, **kw):
            self.assertIs(role, fo.FLOW_VERIFIER_PROMPT)
            self.assertTrue(kw.get("json_mode"))
            prompts.append(prompt)
            return next(replies)

        with patch.object(fo, "ask_agent", side_effect=fake):
            out = fo.verify_flows(self.findings, self.files)

        self.assertEqual(len(prompts), 3)                                       # only findings with a trace
        self.assertIn("db.py:8 find_user: SQL query", prompts[0])               # most severe first, full path
        self.assertIn("conn.execute(sql)", prompts[0])                          # with the code around each step
        by_rule = {f.rule_id: f for f in out}
        self.assertTrue(by_rule["taint-sql"].verdict.startswith("confirmed:"))
        self.assertEqual(by_rule["taint-sql"].severity, "high")
        self.assertTrue(by_rule["taint-command"].verdict.startswith("false_positive:"))
        self.assertEqual(by_rule["taint-command"].severity, "info")             # drops out of must-fix
        self.assertTrue(by_rule["taint-path"].verdict.startswith("needs_context: verification failed"))
        self.assertEqual(by_rule["no-trace"].verdict, "")
        self.assertEqual([f.rule_id for f in out], [f.rule_id for f in self.findings])   # order preserved


if __name__ == "__main__":
    unittest.main()
