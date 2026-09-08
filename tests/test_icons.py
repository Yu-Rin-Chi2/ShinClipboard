import unittest

from shinclipboard.editor import TOOLS
from shinclipboard.icons import ICON_SIZE, tool_icon


class IconTests(unittest.TestCase):
    def test_every_tool_has_a_visible_icon_of_the_right_size(self):
        for name, _label, _hint in TOOLS:
            icon = tool_icon(name)
            self.assertEqual(icon.size, (ICON_SIZE, ICON_SIZE), name)
            self.assertEqual(icon.mode, "RGBA", name)
            opaque = sum(1 for pixel in icon.getdata() if pixel[3] > 0)
            self.assertGreater(opaque, 10, f"the {name} icon is blank")

    def test_icons_differ_from_each_other(self):
        rendered = {tool_icon(name).tobytes() for name, _label, _hint in TOOLS}
        self.assertEqual(len(rendered), len(TOOLS), "two tools share the same picture")

    def test_an_unknown_tool_yields_an_empty_icon_instead_of_raising(self):
        icon = tool_icon("no-such-tool")
        self.assertEqual(icon.size, (ICON_SIZE, ICON_SIZE))
        self.assertTrue(all(pixel[3] == 0 for pixel in icon.getdata()))


if __name__ == "__main__":
    unittest.main()
