"""Geometry checks for the Canvas account cards without opening the UI."""

from pathlib import Path
from types import SimpleNamespace
import unittest

from switcher_ui import CARD_HEIGHT, CARD_STEP, SwitcherApp


class FakeCanvas:
    def __init__(self) -> None:
        self.next_id = 1
        self.objects = {}
        self.options = {}
        self.config = {}

    def delete(self, _tag: str) -> None:
        self.objects.clear()
        self.options.clear()

    def winfo_width(self) -> int:
        return 780

    def configure(self, **kwargs) -> None:
        self.config.update(kwargs)

    def create_rectangle(self, x0, y0, x1, y1, **kwargs) -> int:
        return self._create((x0, y0, x1, y1), kwargs)

    def create_text(self, x, y, **kwargs) -> int:
        return self._create((x, y), kwargs)

    def _create(self, coords, options) -> int:
        item_id = self.next_id
        self.next_id += 1
        self.objects[item_id] = coords
        self.options[item_id] = options
        return item_id

    def tag_bind(self, *_args, **_kwargs) -> None:
        pass


class CompactCardLayoutTests(unittest.TestCase):
    def test_cards_fit_canvas_and_scrollregion_covers_last_row_at_minimum_width(self) -> None:
        app = SwitcherApp.__new__(SwitcherApp)
        app.canvas = FakeCanvas()
        app.colors = {"card": "", "border": "", "accent": "", "muted": "", "text": "", "secondary": "", "panel": "", "bar_bg": "", "warning": ""}
        app.accounts = [SimpleNamespace(account_id=f"synthetic-{i}", email=f"synthetic-{i}@example.invalid") for i in range(3)]
        app.cards = {account.account_id: {"account": account, "items": {}} for account in app.accounts}
        app.core = SimpleNamespace(credentials_dir=Path("synthetic"))
        app.account_refreshing = set()
        app.card_fonts = {"email": object()}
        app.update_cards = lambda: None
        app.fit_card_text = lambda text, _font, width: text[: max(width // 8, 1)]

        app.redraw_cards()

        card_bounds = []
        for account in app.accounts:
            items = app.cards[account.account_id]["items"]
            x0, y0, x1, y1 = app.canvas.objects[items["card"]]
            card_bounds.append((x0, y0, x1, y1))
            button_x = app.canvas.objects[items["button"]][0]
            self.assertLessEqual(app.canvas.objects[items["updated"]][0] + items["updated_width"], button_x)
            self.assertLessEqual(app.canvas.objects[items["error"]][0] + items["error_width"], button_x)
            self.assertLessEqual(app.canvas.objects[items["button"]][2], x1)
            refresh = app.canvas.objects[items["refresh_button"]]
            switch = app.canvas.objects[items["button"]]
            self.assertGreaterEqual(refresh[0], x0)
            self.assertLessEqual(refresh[2], x1)
            self.assertLessEqual(refresh[3], y1)
            self.assertLessEqual(refresh[2], switch[2])
            self.assertLessEqual(refresh[3], switch[1])
            self.assertLessEqual(app.canvas.objects[items["five_panel"]][3], y1)
            self.assertLessEqual(app.canvas.objects[items["weekly_panel"]][3], y1)

            actions = []
            for key in ("rename", "up", "down", "delete"):
                button = app.canvas.objects[items[f"{key}_button"]]
                actions.append(button)
                self.assertGreaterEqual(button[0], x0)
                self.assertLessEqual(button[2], x1)
                self.assertGreaterEqual(button[1], y0)
                self.assertLessEqual(button[3], y1)
                self.assertEqual(button[3] - button[1], 26)
            for previous, current in zip(actions, actions[1:]):
                self.assertLessEqual(previous[2], current[0])
                self.assertEqual(previous[1], current[1])
                self.assertEqual(previous[3], current[3])
            self.assertLessEqual(actions[-1][2], x1 - 12)
            self.assertEqual(actions[0][3], y0 + 144)
            self.assertLessEqual(actions[0][3], y1)

        for previous, current in zip(card_bounds, card_bounds[1:]):
            self.assertLessEqual(previous[3], current[1])
        self.assertEqual(card_bounds[-1][3] + 10, app.canvas.config["scrollregion"][3])
        self.assertEqual(CARD_HEIGHT, card_bounds[0][3] - card_bounds[0][1])
        self.assertEqual(CARD_STEP, card_bounds[1][1] - card_bounds[0][1])


if __name__ == "__main__":
    unittest.main()
