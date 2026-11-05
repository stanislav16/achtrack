try:
    from . import _isolated  # noqa: F401  (before AchTrack: throwaway folders)
except ImportError:
    import _isolated  # noqa: F401
import unittest
from unittest import mock

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from achtrack import gui, motion, notifier
from achtrack.motion import Spring

app = QApplication.instance() or QApplication([])


def run(*springs, seconds=3.0):
    for _ in range(int(seconds * 120)):
        for spring in springs:
            if spring._timer.isActive():        # stepped by hand here, but only while it would be running
                spring.step(1 / 120)


class Springs(unittest.TestCase):
    def test_it_arrives_without_going_past(self):
        spring = Spring(0.0)
        spring.go(100.0)
        highest = 0.0
        for _ in range(360):
            run(spring, seconds=1 / 120)
            highest = max(highest, spring.value)
        self.assertEqual(spring.value, 100.0)
        self.assertLessEqual(highest, 100.0)

    def test_a_new_target_carries_on_from_where_it_is(self):
        spring = Spring(0.0)
        spring.go(100.0)
        run(spring, seconds=0.1)
        where, speed = spring.value, spring.velocity
        self.assertGreater(speed, 0)
        spring.go(0.0)
        spring.step(1 / 1000)
        self.assertAlmostEqual(spring.value, where, delta=abs(speed) / 500)
        self.assertGreater(spring.velocity, 0)      # still heading the old way for a moment: no brick wall

    def test_it_says_when_it_has_settled(self):
        spring = Spring(0.0)
        spring.go(1.0)
        seen = []
        spring.settled.connect(lambda: seen.append(True))
        run(spring)
        self.assertEqual(seen, [True])

    def test_pulling_past_a_limit_gives_less_and_less(self):
        near, far = motion.rubberband(50, 360), motion.rubberband(500, 360)
        self.assertLess(near, 50)
        self.assertLess(far - near, 450 / 2)
        self.assertLess(far, 360 * 0.55)


def mouse(kind, local, screen):
    return QMouseEvent(kind, QPointF(local), QPointF(screen), Qt.MouseButton.LeftButton,
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)


class Popups(unittest.TestCase):
    START, END = QPoint(500, 546), QPoint(500, 500)

    def toast(self, calm=False):
        with mock.patch.object(motion, "reduced", return_value=calm):
            toast = notifier.Toast(notifier.ToastData("Kicker", "Title"), 5, outward=1)
        toast.present(self.START, self.END)
        self.addCleanup(toast.deleteLater)
        self.finished = []
        toast.finished.connect(self.finished.append)
        self.settle(toast)
        return toast

    def settle(self, toast, seconds=3.0):
        run(toast._x, toast._y, toast._shown, toast._press, seconds=seconds)

    def at(self, toast):
        return QPoint(round(toast._x.value), round(toast._y.value))

    def test_it_leaves_the_way_it_came(self):
        toast = self.toast()
        self.assertEqual(self.at(toast), self.END)
        self.assertEqual(toast._shown.value, 1.0)
        toast.dismiss()
        self.settle(toast)
        self.assertAlmostEqual(toast._y.value, self.START.y(), delta=4)     # it's faded out by the very end
        self.assertEqual(self.finished, [toast])

    def test_a_pointer_catches_it_on_the_way_out(self):
        toast = self.toast()
        toast.dismiss()
        self.settle(toast, seconds=0.05)
        toast.enterEvent(QEvent(QEvent.Type.Enter))
        self.settle(toast)
        self.assertFalse(toast.closing)
        self.assertEqual(self.at(toast), self.END)
        self.assertEqual(self.finished, [])

    def test_a_click_is_for_good(self):
        toast = self.toast()
        here, screen = QPoint(50, 50), self.END + QPoint(50, 50)
        toast.mousePressEvent(mouse(QEvent.Type.MouseButtonPress, here, screen))
        self.assertFalse(toast.closing)         # the press only shows; the release decides
        toast.mouseReleaseEvent(mouse(QEvent.Type.MouseButtonRelease, here, screen))
        self.assertTrue(toast.closing)
        toast.enterEvent(QEvent(QEvent.Type.Enter))
        self.assertTrue(toast.closing)

    def test_pressing_then_moving_off_cancels(self):
        toast = self.toast()
        toast.mousePressEvent(mouse(QEvent.Type.MouseButtonPress, QPoint(50, 50), self.END + QPoint(50, 50)))
        toast.mouseReleaseEvent(mouse(QEvent.Type.MouseButtonRelease, QPoint(50, 900), self.END + QPoint(50, 900)))
        self.assertFalse(toast.closing)

    def drag(self, toast, *steps):
        """Drags sideways; each step is (distance so far, seconds since the start)."""
        origin = self.END + QPoint(50, 50)
        toast.mousePressEvent(mouse(QEvent.Type.MouseButtonPress, QPoint(50, 50), origin))
        for moved, when in steps:
            with mock.patch.object(notifier.time, "monotonic", return_value=1000 + when):
                toast.mouseMoveEvent(mouse(QEvent.Type.MouseMove, QPoint(50, 50), origin + QPoint(moved, 0)))
        last = steps[-1][0]
        toast.mouseReleaseEvent(mouse(QEvent.Type.MouseButtonRelease, QPoint(50, 50), origin + QPoint(last, 0)))

    def test_it_follows_a_drag_from_where_it_was_grabbed(self):
        toast = self.toast()
        origin = self.END + QPoint(50, 50)
        toast.mousePressEvent(mouse(QEvent.Type.MouseButtonPress, QPoint(50, 50), origin))
        toast.mouseMoveEvent(mouse(QEvent.Type.MouseMove, QPoint(50, 50), origin + QPoint(4, 0)))
        self.assertEqual(self.at(toast), self.END)      # too small to be a drag yet
        toast.mouseMoveEvent(mouse(QEvent.Type.MouseMove, QPoint(50, 50), origin + QPoint(60, 0)))
        self.assertEqual(self.at(toast), self.END + QPoint(52, 0))     # no jump when the drag begins

    def test_a_flick_throws_it_away(self):
        toast = self.toast()
        self.drag(toast, (20, 0.00), (60, 0.03), (120, 0.06))
        self.assertTrue(toast.closing)
        self.settle(toast)
        self.assertGreater(toast._x.value, self.END.x() + notifier.CARD_W)
        self.assertEqual(self.finished, [toast])

    def test_a_slow_short_drag_comes_back(self):
        toast = self.toast()
        self.drag(toast, (20, 0.0), (40, 0.5), (41, 1.0))
        self.assertFalse(toast.closing)
        self.settle(toast)
        self.assertEqual(self.at(toast), self.END)

    def test_a_long_drag_coming_back_stays(self):
        toast = self.toast()
        self.drag(toast, (300, 0.0), (280, 0.03), (240, 0.06))
        self.assertFalse(toast.closing)

    def test_dragging_inward_resists(self):
        toast = self.toast()
        origin = self.END + QPoint(50, 50)
        toast.mousePressEvent(mouse(QEvent.Type.MouseButtonPress, QPoint(50, 50), origin))
        toast.mouseMoveEvent(mouse(QEvent.Type.MouseMove, QPoint(50, 50), origin - QPoint(208, 0)))
        self.assertGreater(toast._x.value, self.END.x() - 200)

    def test_with_reduced_motion_nothing_travels(self):
        toast = self.toast(calm=True)
        toast.dismiss()
        self.settle(toast, seconds=0.05)
        self.assertEqual(self.at(toast), self.END)
        self.assertLess(toast._shown.value, 1.0)


class Rows(unittest.TestCase):
    def setUp(self):
        self.clicks = []
        self.row = gui.HomeRow(gui.QPixmap(40, 40), "Title", "Sub", "", lambda: self.clicks.append(1))
        self.row.resize(300, 60)

    def press(self, kind, y=10):
        return mouse(kind, QPoint(10, y), QPoint(10, y))

    def test_a_press_shows_before_the_release_acts(self):
        self.row.mousePressEvent(self.press(QEvent.Type.MouseButtonPress))
        self.assertTrue(self.row.property("pressed"))
        self.assertEqual(self.clicks, [])
        self.row.mouseReleaseEvent(self.press(QEvent.Type.MouseButtonRelease))
        self.assertFalse(self.row.property("pressed"))
        self.assertEqual(self.clicks, [1])

    def test_a_hover_is_not_a_press(self):
        # macOS delivers moves with no button held, so this is what pointing at a row looks like.
        self.row.mouseMoveEvent(QMouseEvent(QEvent.Type.MouseMove, QPointF(10, 10), QPointF(10, 10),
                                            Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                                            Qt.KeyboardModifier.NoModifier))
        self.assertFalse(self.row.property("pressed"))

    def test_moving_off_and_letting_go_does_nothing(self):
        self.row.mousePressEvent(self.press(QEvent.Type.MouseButtonPress))
        self.row.mouseMoveEvent(self.press(QEvent.Type.MouseMove, 500))
        self.assertFalse(self.row.property("pressed"))
        self.row.mouseMoveEvent(self.press(QEvent.Type.MouseMove, 10))
        self.assertTrue(self.row.property("pressed"))
        self.row.mouseMoveEvent(self.press(QEvent.Type.MouseMove, 500))
        self.row.mouseReleaseEvent(self.press(QEvent.Type.MouseButtonRelease, 500))
        self.assertEqual(self.clicks, [])


if __name__ == "__main__":
    unittest.main()
