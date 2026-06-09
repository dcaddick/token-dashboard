import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class BurnLayoutTests(unittest.TestCase):
    def test_heatmap_week_columns_expand_to_fill_available_width(self):
        css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")

        self.assertIn("grid-template-columns: 168px minmax(0, 1fr);", css)
        self.assertIn("grid-auto-columns: 13px; gap: 4px; width: 100%;", css)
        self.assertIn("justify-content: space-between;", css)


if __name__ == "__main__":
    unittest.main()
