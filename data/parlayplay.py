"""
data/parlayplay.py — ParlayPlay Fantasy scraper (Playwright-based).

ParlayPlay (parlayplay.io) serves its data via an auth-gated Django API that
rejects Python requests (Cloudflare). We use Playwright to render the page in
a real browser, then inject JS to extract the already-rendered DOM data.

Data structure extracted:
  Each row = one (player, stat, line) entry with:
    - line_score:   float  — the prop line value
    - less_mult:    float  — Less-side decimal multiplier (Left column)
    - more_mult:    float  — More-side decimal multiplier (Right column)
    - is_promo_less / is_promo_more: bool — 🔥 boost on that side

Multiplier semantics (decimal odds, full return including stake):
    1.07x = 93.5% implied prob (very easy)
    1.65x = 60.6% (moderate)
    2.12x = 47.2% (coin-flip)
    3.78x = 26.5% (hard)

The "standard" balanced line for each (player, stat) is where
|more_mult - less_mult| is smallest (mults are closest to equal).

Returned grouped format (same convention as underdog.py):
  Key: (player_name, stat)
  Value: {
    "standard": float | None,   # most balanced line_score
    "lines":    list[dict],     # all lines sorted by line_score ascending
  }
  Each line_dict: {line_score, less_mult, more_mult, is_promo_less, is_promo_more}
"""

import json
from pathlib import Path

# ── JS extractor injected into the loaded page ────────────────────────────────

_EXTRACT_JS = r"""
() => {
  const STAT_HEADER_SEL = '[class="grow h-full px-2 pt-2 text-center text-textSecondary"]';
  const ROW_CLASS       = 'flex flex-row items-center justify-between w-full h-8 my-1 text-sm w-divl text-grey-vDark';
  const PLAYER_NAME_CLS = 'w-full text-base text-start font-display';

  function parseMult(t) {
    const m = String(t || '').match(/(\d+\.?\d*)/);
    return m ? parseFloat(m[1]) : null;
  }

  const allEls = [];
  const sel = `[class="${PLAYER_NAME_CLS}"], ${STAT_HEADER_SEL}, [class="${ROW_CLASS}"]`;
  document.querySelectorAll(sel).forEach(el => {
    if (el.matches(`[class="${PLAYER_NAME_CLS}"]`)) {
      allEls.push({ type: 'player', text: (el.innerText || '').trim() });
    } else if (el.matches(STAT_HEADER_SEL)) {
      allEls.push({ type: 'stat', text: (el.innerText || '').trim() });
    } else {
      const ch = Array.from(el.children);
      if (ch.length !== 3) return;
      const lineText = (ch[1].innerText || '').trim();
      const lm = lineText.match(/^(\d+\.?\d*)/);
      if (!lm) return;
      allEls.push({
        type:          'row',
        line_score:    parseFloat(lm[1]),
        less_mult:     parseMult(ch[0].innerText),
        more_mult:     parseMult(ch[2].innerText),
        is_promo_less: (ch[0].innerText || '').includes('\uD83D\uDD25'),
        is_promo_more: (ch[2].innerText || '').includes('\uD83D\uDD25'),
      });
    }
  });

  const results = [];
  let curPlayer = '', curStat = '';
  for (const el of allEls) {
    if (el.type === 'player') { curPlayer = el.text; curStat = ''; }
    else if (el.type === 'stat') { curStat = el.text; }
    else if (el.type === 'row' && curPlayer && curStat) {
      results.push({
        player:        curPlayer,
        stat:          curStat,
        line_score:    el.line_score,
        less_mult:     el.less_mult,
        more_mult:     el.more_mult,
        is_promo_less: el.is_promo_less,
        is_promo_more: el.is_promo_more,
      });
    }
  }
  return results;
}
"""

# ── Fetch via Playwright ───────────────────────────────────────────────────────

def _fetch_via_playwright(timeout_ms: int = 30_000, scroll_passes: int = 15) -> list[dict]:
    """
    Open parlayplay.io in an off-screen non-headless Chrome window (required to
    bypass Cloudflare bot detection), scroll to trigger lazy-loading, then
    extract structured data via injected JS.

    Parameters
    ----------
    timeout_ms   : page load + element wait timeout in ms
    scroll_passes: number of End-key presses to trigger lazy-load content
    """
    import time
    from playwright.sync_api import sync_playwright

    row_sel = (
        '[class="flex flex-row items-center justify-between '
        'w-full h-8 my-1 text-sm w-divl text-grey-vDark"]'
    )

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            channel="chrome",
            headless=False,           # headless blocked by Cloudflare
            args=[
                "--window-position=-2000,-2000",  # move off-screen
                "--window-size=1280,900",
            ],
        )
        ctx  = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.goto("https://parlayplay.io/", wait_until="domcontentloaded",
                  timeout=timeout_ms)

        # Wait for first batch of rows
        page.wait_for_selector(row_sel, timeout=timeout_ms)

        # Scroll the main scrollable container to trigger lazy-loading.
        # ParlayPlay uses a virtualized list; we must scroll progressively.
        scroll_js = """
        async () => {
            // The player-card list is in col-span-5 overflow-y-auto.
            // It uses infinite scroll: scrollHeight grows as we scroll down.
            // Keep scrolling step by step until scrollTop can't increase anymore.
            const scroller = document.querySelector('.col-span-5.overflow-y-auto') ||
                             Array.from(document.querySelectorAll('*')).find(el => {
                               const s = window.getComputedStyle(el);
                               return (s.overflowY==='auto'||s.overflowY==='scroll')
                                      && el.scrollHeight > 5000;
                             }) ||
                             document.documentElement;
            const step = 1200;
            const delay = ms => new Promise(r => setTimeout(r, ms));
            let lastTop = -1;
            let stuckCount = 0;
            while (stuckCount < 3) {
                scroller.scrollTop += step;
                await delay(400);
                if (scroller.scrollTop === lastTop) {
                    stuckCount++;
                } else {
                    stuckCount = 0;
                    lastTop = scroller.scrollTop;
                }
            }
            scroller.scrollTop = 0;
            await delay(500);
            return { finalScrollHeight: scroller.scrollHeight, finalTop: lastTop };
        }
        """
        result = page.evaluate(scroll_js)
        print(f"[parlayplay] Scrolled to end (scrollHeight={result.get('finalScrollHeight')}, finalTop={result.get('finalTop')})")
        time.sleep(2)  # let final renders settle

        rows: list[dict] = page.evaluate(_EXTRACT_JS)
        browser.close()
        return rows


def _group(rows: list[dict]) -> dict:
    """
    Group flat row list into:
        { (player, stat): { "standard": float|None, "lines": [line_dict, ...] } }
    """
    grouped: dict = {}
    for r in rows:
        key = (r["player"], r["stat"])
        if key not in grouped:
            grouped[key] = {"lines": []}
        grouped[key]["lines"].append({
            "line_score":    r["line_score"],
            "less_mult":     r["less_mult"],
            "more_mult":     r["more_mult"],
            "is_promo_less": r["is_promo_less"],
            "is_promo_more": r["is_promo_more"],
        })

    for info in grouped.values():
        info["lines"].sort(key=lambda l: l["line_score"])
        # Standard = most balanced line (|more - less| smallest), ignoring promos
        best_diff, best_score = None, None
        for ln in info["lines"]:
            if ln["less_mult"] and ln["more_mult"]:
                d = abs(ln["more_mult"] - ln["less_mult"])
                if best_diff is None or d < best_diff:
                    best_diff = d
                    best_score = ln["line_score"]
        info["standard"] = best_score

    return grouped


def get_grouped_lines(cache_path: str | Path | None = None) -> tuple[dict, list[dict]]:
    """
    Fetch ParlayPlay lines and group by (player_name, stat).

    Parameters
    ----------
    cache_path : str or Path, optional
        If provided, save raw rows to this JSON path for debugging.

    Returns
    -------
    grouped : dict — {(player, stat): {"standard": float|None, "lines": [...]}}
    raw     : list — flat row list from DOM extraction
    """
    print("[parlayplay] Launching Playwright to fetch parlayplay.io...")
    rows = _fetch_via_playwright()
    print(f"[parlayplay] Extracted {len(rows)} prop rows from {len(set(r['player'] for r in rows))} players")

    if cache_path:
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        Path(cache_path).write_text(json.dumps(rows, indent=2))
        print(f"[parlayplay] Raw rows saved → {cache_path}")

    grouped = _group(rows)
    return grouped, rows


# ── Quick test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    grouped, raw = get_grouped_lines(cache_path="logs/parlayplay_raw.json")

    stats_count: dict[str, int] = {}
    for _, stat in grouped:
        stats_count[stat] = stats_count.get(stat, 0) + 1

    print(f"\n{len(grouped)} (player, stat) combos across {len(stats_count)} stat types:")
    for stat, n in sorted(stats_count.items(), key=lambda x: -x[1]):
        print(f"  {n:4d}  {stat}")

    print("\nSample entries:")
    for (player, stat), info in list(grouped.items())[:3]:
        std = info["standard"]
        lines = info["lines"]
        print(f"\n  {player} — {stat}  (standard={std})")
        for ln in lines[:5]:
            promo = "🔥" if (ln["is_promo_less"] or ln["is_promo_more"]) else "  "
            std_tag = " ← std" if ln["line_score"] == std else ""
            print(f"    {promo} Less={ln['less_mult']}x | {ln['line_score']} | More={ln['more_mult']}x{std_tag}")
