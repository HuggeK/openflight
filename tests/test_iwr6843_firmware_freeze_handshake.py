"""The self-trigger's freeze/release handshake, firmware/iwr6843/l3_dump.c.

2026-09-30, the acceptance run on the board: between the verify and the dss
phases the board locked up. Its timeline showed the frame right after a
rearm (slot 0, epoch 1) followed at once by a post-impact tail (slots 9-23),
no swing, then nothing: frozen, not latched, so nothing rearmed it.

The mechanism: every completed freeze posts gHwaFreezeSemaphore (binary).
l3release on a latched board waited on it only while the capture still
ran, so a ring that froze before the release left its post stale. At the
next fire a release arriving mid-tail found that stale post, stopped the
capture before the ISR reached the freeze boundary, and left
gHwaFreezeRequested set; the rearm did not clear it, so the first frame
after the rearm began a new post capture, which froze unlatched.

These pin the three fixes; the race itself cannot run on the host.

2026-10-29, the range session with --debug: l3dump requested a second freeze
over the self-trigger's, restarting the post tail, and left the latch set. The
next l3release, on a board already running again, waited out a freeze that
never came and the host restarted the radar after 240 of 244 triggers.
"""

from __future__ import annotations

from pathlib import Path

SOURCE = Path(__file__).parents[1] / "firmware" / "iwr6843" / "l3_dump.c"
DRAIN = "while (Semaphore_pend(gHwaFreezeSemaphore, BIOS_NO_WAIT))"


def _function(signature: str) -> str:
    text = SOURCE.read_text(encoding="utf-8")
    body = text[text.index(signature + "\n{") :]
    return body[: body.index("\n}\n")]


def test_a_fire_drains_stale_freeze_completions_before_requesting_its_freeze():
    trigger = _function("static void l3_considerSelfTrigger(uint32_t slot)")
    request = trigger.index("gHwaFreezeRequested = 1U;")
    assert DRAIN in trigger[:request]


def test_a_release_consumes_the_completion_of_a_ring_that_already_froze():
    wait = _function("static int32_t l3_awaitFrozenRing(void)")
    latched = wait[wait.index("if (gSelfTriggerLatched) {") :]
    latched = latched[: latched.index("return l3_finishCaptureStop();")]
    assert "else if (gHwaFreezeSemaphore != NULL)" in latched
    assert "(void)Semaphore_pend(gHwaFreezeSemaphore, BIOS_NO_WAIT);" in latched


def test_a_rearm_clears_any_pending_freeze_request_before_the_front_end_restarts():
    rearm = _function("static int32_t l3_sparseRearm(void)")
    clear = rearm.index("gHwaFreezeRequested = 0U;")
    assert clear < rearm.index("l3_startFrontEnd()")
    assert clear < rearm.index("l3_restartCompletedHwaFrame()")


def test_a_dump_takes_the_self_triggers_freeze_like_the_sparse_reads():
    """Through l3_awaitFrozenRing, which clears the latch and waits on the
    fire's own freeze: a second request would restart its post tail."""
    dump = _function("int32_t l3_cli_dump(int32_t argc, char *argv[])")
    assert "l3_awaitFrozenRing()" in dump
    assert "l3_stopCaptureAtBoundary()" not in dump


def test_a_dump_of_a_frozen_ring_is_not_refused():
    """A freeze clears gCaptureActive, so a dump reaching a finished tail must
    get past the entry check too; l3_awaitFrozenRing owns that check."""
    dump = _function("int32_t l3_cli_dump(int32_t argc, char *argv[])")
    assert "if (!gCaptureActive)" not in dump
