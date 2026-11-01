"""The prices these docs quote are the ones Phone Harness Cloud charges.

phone-cloud owns the numbers: production's settings, and tests/test_pricing_drift.py
there, which also checks this repository when PHONE_HARNESS_DIR names a checkout.
This holds the docs still between those runs. Change a price there first.

    python -m unittest discover tests -p test_pricing_copy.py
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ("README.md", "SKILL.md", "install.md", "onboarding.md", "setup/cloud.md",
        "integrations/hermes/README.md")

RATE, GRANT, TRIAL_MINUTES = "5¢", "$5", "100"
IPHONE = {"$80", "$100", "$768", "$960"}
FIGURES = {RATE, GRANT, *IPHONE}
FIGURE = re.compile(r"(?<![\w$])\$\d+(?:,\d{3})*(?:\.\d+)?|(?<![\w.])\d+(?:\.\d+)?¢")


def read(name):
    return " ".join((ROOT / name).read_text(encoding="utf-8").split())


class PricingCopy(unittest.TestCase):
    def test_every_figure_is_a_published_price(self):
        for name in DOCS:
            with self.subTest(doc=name):
                self.assertEqual(set(FIGURE.findall(read(name))) - FIGURES, set())

    def test_the_starting_credit_and_the_minutes_it_buys(self):
        for name in ("install.md", "onboarding.md", "setup/cloud.md"):
            text = read(name)
            self.assertIn(f"{GRANT} of credit", text, name)
            self.assertRegex(text, rf"\b{TRIAL_MINUTES} (?:Android )?minutes", name)

    def test_a_cloud_iphone_is_a_subscription_and_not_billed_by_the_minute(self):
        for name in ("SKILL.md", "install.md", "onboarding.md", "setup/cloud.md",
                     "integrations/hermes/README.md"):
            text = read(name)
            self.assertIn("flat subscription", text, name)
            self.assertNotRegex(text, r"(?i)iphone[^.|]*\b(not charged|is free)\b", name)
        self.assertLessEqual(IPHONE, set(FIGURE.findall(read("setup/cloud.md"))))


if __name__ == "__main__":
    unittest.main()
