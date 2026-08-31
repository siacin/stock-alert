from __future__ import annotations

import unittest
from tkinter import TclError
from unittest.mock import Mock

from widget import MiniStockWidget


class WidgetCloseTests(unittest.TestCase):
    def setUp(self):
        self.widget = MiniStockWidget.__new__(MiniStockWidget)
        self.widget.root = Mock()
        self.widget.closing = False
        self.widget._get_json = Mock(return_value={})

    def test_close_is_idempotent_and_does_not_call_the_backend(self):
        self.widget._close()
        self.widget._close()
        self.assertTrue(self.widget.closing)
        self.widget.root.destroy.assert_called_once_with()
        self.widget._get_json.assert_not_called()

    def test_late_status_and_trend_results_do_not_touch_closed_window(self):
        self.widget._close()
        self.widget._poll_worker()
        self.widget._trend_worker("600000")
        self.widget.root.after.assert_not_called()
        self.assertFalse(self.widget.fetching)

    def test_close_racing_with_a_worker_callback_is_safe(self):
        for error in (RuntimeError("main loop is closed"), TclError("window is destroyed")):
            with self.subTest(error=type(error).__name__):
                self.widget.root.after.side_effect = error
                self.widget._after_if_open(Mock())


if __name__ == "__main__":
    unittest.main()
