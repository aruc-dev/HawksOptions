import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Optional


ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = ROOT / "scheduler" / "systemd"


class SystemdSecretsTests(unittest.TestCase):
    def test_trading_jobs_do_not_require_secrets_service(self):
        for name in ["scan", "risk-check", "risk-watch", "roll-check", "eod-report"]:
            unit = SYSTEMD_DIR / f"hawksoptions-{name}.service"
            text = unit.read_text(encoding="utf-8")
            self.assertNotIn("Requires=hawksoptions-secrets.service", text, unit.name)
            self.assertIn("Wants=network-online.target hawksoptions-secrets.service", text, unit.name)
            self.assertIn("After=network-online.target hawksoptions-secrets.service", text, unit.name)

    def test_secrets_service_accepts_forced_refresh_and_fixes_legacy_file_owner(self):
        text = (SYSTEMD_DIR / "hawksoptions-secrets.service").read_text(encoding="utf-8")
        self.assertIn("PassEnvironment=HAWKSOPTIONS_SECRETS_FORCE_REFRESH", text)
        self.assertIn("HAWKSOPTIONS_SECRETS_MAX_AGE_SECONDS", text)
        self.assertIn("ExecStartPre=+", text)
        self.assertIn("chown ec2-user:ec2-user", text)

    def test_secrets_timer_refreshes_independently(self):
        text = (SYSTEMD_DIR / "hawksoptions-secrets.timer").read_text(encoding="utf-8")
        self.assertIn("OnBootSec=1min", text)
        self.assertIn("OnCalendar=*:0/30", text)
        self.assertIn("Unit=hawksoptions-secrets.service", text)

    def test_fetch_secrets_script_has_freshness_guard_and_valid_syntax(self):
        script = ROOT / "scripts" / "fetch_secrets.sh"
        text = script.read_text(encoding="utf-8")
        self.assertIn("HAWKSOPTIONS_SECRETS_MAX_AGE_SECONDS", text)
        self.assertIn("HAWKSOPTIONS_SECRETS_FORCE_REFRESH", text)
        self.assertIn("reusing existing", text)
        self.assertIn("reusing fresh", text)
        subprocess.run(["bash", "-n", str(script)], check=True)

    def test_prune_reports_service_matches_trading_job_layout(self):
        text = (SYSTEMD_DIR / "hawksoptions-prune-reports.service").read_text(encoding="utf-8")
        self.assertIn("User=ec2-user", text)
        self.assertIn("Group=ec2-user", text)
        self.assertIn("WorkingDirectory=/home/ec2-user/HawksOptions", text)
        self.assertIn("ExecStart=/home/ec2-user/HawksOptions/scripts/prune_reports.sh", text)
        self.assertNotIn("/opt/hawksoptions", text)

    def test_kill_script_creates_halt_file_with_restrictive_umask(self):
        script = ROOT / "scripts" / "kill.sh"
        text = script.read_text(encoding="utf-8")
        self.assertIn("umask 077", text)
        subprocess.run(["bash", "-n", str(script)], check=True)

    def test_prune_reports_script_passes_filenames_after_option_separator(self):
        script = ROOT / "scripts" / "prune_reports.sh"
        text = script.read_text(encoding="utf-8")
        self.assertIn("gzip -9 --", text)
        self.assertIn("rm -f --", text)
        subprocess.run(["bash", "-n", str(script)], check=True)

    def _write_valid_existing_file(self, path: str):
        Path(path).write_text(
            "ALPACA_OPTIONS_PAPER_API_KEY='old-key'\n"
            "ALPACA_OPTIONS_PAPER_SECRET_KEY='old-secret'\n",
            encoding="utf-8",
        )

    def _write_fake_aws_tools(self, bindir: Path, counter: Path):
        (bindir / "aws").write_text(
            "#!/usr/bin/env bash\n"
            f"echo called >> {counter}\n"
            "printf '%s\\n' '{\"ALPACA_OPTIONS_PAPER_API_KEY\":\"new-key\",\"ALPACA_OPTIONS_PAPER_SECRET_KEY\":\"new-secret\"}'\n",
            encoding="utf-8",
        )
        (bindir / "jq").write_text(
            "#!/usr/bin/env bash\n"
            "key=''\n"
            "while [ \"$#\" -gt 0 ]; do\n"
            "  if [ \"$1\" = '--arg' ] && [ \"${2:-}\" = 'k' ]; then key=\"$3\"; shift 3; else shift; fi\n"
            "done\n"
            "case \"$key\" in\n"
            "  ALPACA_OPTIONS_PAPER_API_KEY) printf \"'new-key'\\n\" ;;\n"
            "  ALPACA_OPTIONS_PAPER_SECRET_KEY) printf \"'new-secret'\\n\" ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        os.chmod(bindir / "aws", 0o755)
        os.chmod(bindir / "jq", 0o755)

    def _run_fetch_with_fake_aws(self, output_file: str, extra_env: Optional[dict[str, str]] = None):
        script = ROOT / "scripts" / "fetch_secrets.sh"
        with tempfile.TemporaryDirectory() as tmpdir:
            bindir = Path(tmpdir) / "bin"
            bindir.mkdir()
            counter = Path(tmpdir) / "aws-count"
            self._write_fake_aws_tools(bindir, counter)
            env = {
                "HAWKSOPTIONS_SHM_SECRET_FILE": output_file,
                "PATH": f"{bindir}:/usr/bin:/bin",
            }
            if extra_env:
                env.update(extra_env)
            result = subprocess.run(
                ["bash", str(script)],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )
            call_count = counter.read_text(encoding="utf-8").count("called") if counter.exists() else 0
        return result, call_count

    def test_fetch_secrets_reuses_existing_ram_file_by_default(self):
        script = ROOT / "scripts" / "fetch_secrets.sh"
        with tempfile.NamedTemporaryFile("w", delete=True) as tmp:
            tmp.write(
                "ALPACA_OPTIONS_PAPER_API_KEY='x'\n"
                "ALPACA_OPTIONS_PAPER_SECRET_KEY='y'\n"
            )
            tmp.flush()
            result = subprocess.run(
                ["bash", str(script)],
                check=True,
                capture_output=True,
                text=True,
                env={
                    "HAWKSOPTIONS_SHM_SECRET_FILE": tmp.name,
                    "PATH": "/usr/bin:/bin",
                },
            )
        self.assertIn("reusing existing", result.stdout)

    def test_fetch_secrets_accepts_leading_zero_max_age_as_base_10(self):
        script = ROOT / "scripts" / "fetch_secrets.sh"
        with tempfile.NamedTemporaryFile("w", delete=True) as tmp:
            tmp.write(
                "ALPACA_OPTIONS_PAPER_API_KEY='x'\n"
                "ALPACA_OPTIONS_PAPER_SECRET_KEY='y'\n"
            )
            tmp.flush()
            result = subprocess.run(
                ["bash", str(script)],
                check=True,
                capture_output=True,
                text=True,
                env={
                    "HAWKSOPTIONS_SHM_SECRET_FILE": tmp.name,
                    "HAWKSOPTIONS_SECRETS_MAX_AGE_SECONDS": "08",
                    "PATH": "/usr/bin:/bin",
                },
            )
        self.assertIn("reusing fresh", result.stdout)

    def test_fetch_secrets_refreshes_stale_file_when_max_age_is_set(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as tmp:
            output_file = tmp.name
        self.addCleanup(lambda: Path(output_file).unlink(missing_ok=True))
        self._write_valid_existing_file(output_file)
        old = time.time() - 30
        os.utime(output_file, (old, old))

        result, call_count = self._run_fetch_with_fake_aws(
            output_file,
            {"HAWKSOPTIONS_SECRETS_MAX_AGE_SECONDS": "1"},
        )

        self.assertEqual(call_count, 1)
        self.assertIn("wrote", result.stdout)
        self.assertIn("new-key", Path(output_file).read_text(encoding="utf-8"))

    def test_fetch_secrets_force_refresh_bypasses_reuse(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as tmp:
            output_file = tmp.name
        self.addCleanup(lambda: Path(output_file).unlink(missing_ok=True))
        self._write_valid_existing_file(output_file)

        result, call_count = self._run_fetch_with_fake_aws(
            output_file,
            {"HAWKSOPTIONS_SECRETS_FORCE_REFRESH": "1"},
        )

        self.assertEqual(call_count, 1)
        self.assertIn("wrote", result.stdout)
        self.assertIn("new-secret", Path(output_file).read_text(encoding="utf-8"))

    def test_fetch_secrets_refreshes_existing_file_missing_required_keys(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as tmp:
            output_file = tmp.name
            tmp.write("# HawksOptions secrets\n")
        self.addCleanup(lambda: Path(output_file).unlink(missing_ok=True))

        result, call_count = self._run_fetch_with_fake_aws(output_file)

        self.assertEqual(call_count, 1)
        self.assertIn("missing required keys; refreshing", result.stdout)
        self.assertIn("new-key", Path(output_file).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
