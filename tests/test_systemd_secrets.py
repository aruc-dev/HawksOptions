import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = ROOT / "scheduler" / "systemd"


class SystemdSecretsTests(unittest.TestCase):
    def test_trading_jobs_do_not_require_secrets_service(self):
        for name in ["scan", "risk-check", "risk-watch", "roll-check", "eod-report"]:
            unit = SYSTEMD_DIR / f"hawksoptions-{name}.service"
            text = unit.read_text(encoding="utf-8")
            self.assertNotIn("Requires=hawksoptions-secrets.service", text, unit.name)
            self.assertIn("After=network-online.target hawksoptions-secrets.service", text, unit.name)

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

    def test_fetch_secrets_reuses_existing_ram_file_by_default(self):
        script = ROOT / "scripts" / "fetch_secrets.sh"
        with tempfile.NamedTemporaryFile("w", delete=True) as tmp:
            tmp.write("ALPACA_OPTIONS_PAPER_API_KEY='x'\n")
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


if __name__ == "__main__":
    unittest.main()
