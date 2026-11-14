"""Ensure both skill entry points resolve into the same portable package."""
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
PACKAGE = ROOT / "skills" / "souvenir"


class PackageTests(unittest.TestCase):
    def test_all_markdown_links_resolve_locally(self):
        files = [ROOT / "README.md", *(PACKAGE.rglob("*.md")),
                 *((ROOT / ".agents" / "skills" / "souvenir").rglob("*.md"))]
        for file in files:
            for target in re.findall(r"\]\(([^)]+)\)", file.read_text(encoding="utf-8")):
                if "://" in target or target.startswith("#"):
                    continue
                target = target.split("#", 1)[0]
                with self.subTest(file=str(file.relative_to(ROOT)), target=target):
                    self.assertTrue((file.parent / target).is_file())

    def test_plugin_entry_and_media_exist(self):
        plugin = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
        extension = plugin["extensions"]["com.openai"]
        self.assertTrue(extension["requires_local_executor"])
        for target in (extension["onboardingSkill"], extension["interface"]["logo"],
                       extension["interface"]["composerIcon"]):
            self.assertTrue((ROOT / target).is_file())
        self.assertTrue((PACKAGE / "scripts" / "run_souvenir.py").is_file())
        marketplace = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text(encoding="utf-8"))
        self.assertEqual(marketplace["plugins"][0]["name"], plugin["name"])

    def test_packaged_skill_references_do_not_require_local_checkout(self):
        for file in PACKAGE.rglob("*.md"):
            for target in re.findall(r"\]\(([^)]+)\)", file.read_text(encoding="utf-8")):
                if "://" in target or target.startswith("#"):
                    continue
                resolved = (file.parent / target.split("#", 1)[0]).resolve()
                with self.subTest(file=file.name, target=target):
                    self.assertIn(PACKAGE, resolved.parents)


if __name__ == "__main__":
    unittest.main()
