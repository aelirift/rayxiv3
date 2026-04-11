"""Boot sf2_rage headless, drive inputs, capture [trace] console lines, report."""
import re
import sys
from pathlib import Path
from collections import defaultdict


def main():
    from playwright.sync_api import sync_playwright

    trace_events: list[str] = []
    errors: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1280, "height": 720})
        page = ctx.new_page()

        def on_console(msg):
            txt = msg.text
            if "[trace]" in txt:
                trace_events.append(txt)
            elif msg.type in ("error",):
                errors.append(f"[{msg.type}] {txt}")

        page.on("console", on_console)
        page.on("pageerror", lambda e: errors.append(f"[pageerror] {e}"))

        print("→ Load page")
        page.goto("https://localhost:8443/godot/sf2_rage/",
                   wait_until="domcontentloaded", timeout=30000)
        page.wait_for_selector("canvas", timeout=15000)
        page.wait_for_timeout(8000)

        canvas = page.query_selector("canvas")
        if canvas:
            canvas.click()
        page.wait_for_timeout(500)

        # Baseline: record how many traces fired just from scene load
        baseline_count = len(trace_events)
        print(f"→ After load: {baseline_count} trace events")

        # Exercise inputs in order
        sequence = [
            ("walk right", [("down", "d"), ("wait", 1000), ("up", "d")]),
            ("walk left",  [("down", "a"), ("wait", 1000), ("up", "a")]),
            ("jump",       [("press", "w"), ("wait", 1200)]),
            ("crouch",     [("down", "s"), ("wait", 600), ("up", "s")]),
            ("light punch",[("press", "u"), ("wait", 800)]),
            ("heavy punch",[("press", "o"), ("wait", 900)]),
            ("light kick", [("press", "j"), ("wait", 800)]),
            ("heavy kick", [("press", "l"), ("wait", 900)]),
            ("hadouken (qcf+u)", [
                ("down", "s"), ("wait", 80),
                ("down", "d"), ("wait", 80),
                ("up", "s"),   ("wait", 40),
                ("press", "u"), ("wait", 1500),
                ("up", "d"),
            ]),
            ("wait for cpu",[("wait", 4000)]),
        ]

        per_step_counts = {}
        for label, actions in sequence:
            before = len(trace_events)
            for kind, arg in actions:
                if kind == "down":
                    page.keyboard.down(arg)
                elif kind == "up":
                    page.keyboard.up(arg)
                elif kind == "press":
                    page.keyboard.press(arg)
                elif kind == "wait":
                    page.wait_for_timeout(int(arg))
            after = len(trace_events)
            per_step_counts[label] = after - before
            print(f"→ {label:<25} fired {after-before:4d} new traces")

        browser.close()

    # Save full log
    log_path = Path(".debug/sf2_rage_traces.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("\n".join(trace_events))
    print(f"\nSaved {len(trace_events)} total trace events to {log_path}")
    print(f"Errors: {len(errors)}")
    for e in errors[:10]:
        print(f"  {e[:200]}")

    # Event taxonomy — which <system>.<event> tags appeared, how often
    pattern = re.compile(r"\[trace\]\s+(\w+)\.(\w+)")
    histogram: dict[tuple[str, str], int] = defaultdict(int)
    for line in trace_events:
        m = pattern.search(line)
        if m:
            histogram[(m.group(1), m.group(2))] += 1

    print(f"\nTrace event histogram ({len(histogram)} distinct <system>.<event> tags):")
    print(f"  {'system.event':<40} {'count'}")
    print(f"  {'-'*40} -----")
    for (sys_name, ev_name), count in sorted(histogram.items(), key=lambda x: (-x[1], x[0])):
        print(f"  {sys_name+'.'+ev_name:<40} {count}")


if __name__ == "__main__":
    main()
