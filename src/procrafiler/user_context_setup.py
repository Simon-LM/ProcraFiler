"""Interactive `setup-context` questionnaire: a guided, UNIVERSAL way to produce
the user-context file the AI reads — for ANY user (employee, self-employed,
student, retiree, parent…), not a fixed profile.

Core principle: the context GUIDES, it never CONSTRAINS. The document's content
still decides; the declared facts only help disambiguate and stay consistent. A
user will forget some hobbies and can never list every project/client (new ones
appear), so the prompts treat these as HINTS, not a closed list (see
`ai_analysis` / `ai_organize`).

The answers are rendered to a plain-text context file read by
`user_context.load_user_context`; the user never edits a config format by hand.
It is personal data: gitignored, never committed. I/O is injected (`ask` / `out`)
so the flow is fully testable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from procrafiler.user_context import active_context_path, default_context_write_path

AskFn = Callable[[str], str]
OutFn = Callable[[str], None]

# A short, universal interest list; anything else is typed freely. NOTHING is
# assumed or created unless the user picks or types it.
INTERESTS: tuple[str, ...] = (
    "Music",
    "Sports",
    "Reading",
    "Cooking",
    "Gardening",
    "Photography",
    "Video games",
    "DIY",
    "Travel",
)

# Common languages → code. Used to enrich the catalog so search works in the
# user's language and English. Anything else can be set later via `language`.
LANGUAGES: tuple[tuple[str, str], ...] = (
    ("Français", "fr"),
    ("English", "en"),
    ("Español", "es"),
    ("Deutsch", "de"),
    ("Italiano", "it"),
    ("Português", "pt"),
)

def _text(ask: AskFn, out: OutFn, label: str) -> str:
    out(label)
    return ask("› ").strip()


def _csv(ask: AskFn, out: OutFn, label: str) -> list[str]:
    out(label)
    out("   (several answers possible — separate them with COMMAS)")
    return [p.strip() for p in ask("› ").split(",") if p.strip()]


def _choice(ask: AskFn, out: OutFn, label: str, options: list[str]) -> int | None:
    out(label)
    for i, opt in enumerate(options, 1):
        out(f"  {i}) {opt}")
    raw = ask("› ").strip()
    return int(raw) - 1 if raw.isdigit() and 1 <= int(raw) <= len(options) else None


def _checklist(ask: AskFn, out: OutFn, options: tuple[str, ...]) -> list[str]:
    for i, opt in enumerate(options, 1):
        out(f"  {i}) {opt}")
    raw = ask("Numbers and/or free words, separated by commas › ")
    selected: list[str] = []
    for token in raw.split(","):
        t = token.strip()
        if not t:
            continue
        val = options[int(t) - 1] if (t.isdigit() and 1 <= int(t) <= len(options)) else t
        if val not in selected:
            selected.append(val)
    return selected


def _language(ask: AskFn, out: OutFn) -> str:
    """Ask the user's main language; return a code (e.g. "fr") or "" if skipped."""
    idx = _choice(ask, out, "Your main language? (search works in it + English)",
                  [name for name, _ in LANGUAGES])
    return LANGUAGES[idx][1] if idx is not None else ""


def collect_answers(ask: AskFn, out: OutFn) -> dict[str, Any]:
    """Run the guided questionnaire and return the raw answers. A filing tool
    handles OLD documents, so most fields are MULTI-VALUE and ask for current AND
    past (jobs, providers, homes…). Everything is skippable with Enter."""
    a: dict[str, Any] = {}

    out("\n1/6 · Your language")
    a["language"] = _language(ask, out)

    out("\n2/6 · You")
    a["first_name"] = _text(ask, out, "First name?")
    a["last_name"] = _text(ask, out, "Last name?")
    a["aliases"] = _csv(ask, out, "Online handle(s)? (to recognise you in screenshots, exports, messages)")

    out("\n3/6 · Your work   (current AND past — you may have old documents)")
    a["professions"] = _csv(ask, out, "Your professions (current and past)?")
    a["employers"] = _csv(ask, out, "Your employers (current and past)?")
    a["businesses"] = _csv(ask, out, "Your own activities / businesses (current and past)?")
    a["work_names"] = _csv(ask, out, "Names that mean \"this is my work\"? (clients, projects, tools, servers)")

    out("\n4/6 · Your interests   (only these folders get created)")
    a["interests"] = _checklist(ask, out, INTERESTS)
    a["online_content"] = _csv(ask, out, "Do you create / publish content online? (videos, posts, streams, podcasts — platforms or handles)")

    out("\n5/6 · Your household   (optional — Enter to skip)")
    a["banks"] = _csv(ask, out, "Bank(s), current and past?")
    a["insurers"] = _csv(ask, out, "Insurer(s)?")
    a["energy"] = _csv(ask, out, "Energy / water — current and past providers?")
    a["telecom"] = _csv(ask, out, "Phone / internet — providers (several possible)?")
    a["rentals"] = _csv(ask, out, "Rented homes, current and past? (city or street — to recognise leases, rent receipts, inventories)")
    a["properties"] = _csv(ask, out, "Properties you own / have owned? (city or street — to recognise deeds, property tax, mortgage)")
    a["vehicles"] = _csv(ask, out, "Vehicle(s)? (e.g. car, motorbike)")
    a["household"] = _csv(ask, out, "First names in your household (partner, children…)?")

    out("\n6/6 · Anything else to know?")
    a["notes"] = _text(ask, out, "(one sentence, or Enter)")
    return a


def _line(lines: list[str], label: str, values: Any) -> None:
    """Append `label: a, b, c.` when `values` (a list) is non-empty."""
    if values:
        lines.append(f"{label}: " + ", ".join(values) + ".")


def render_context(a: dict[str, Any]) -> str:
    """Render the answers into the plain-text context the AI reads. English
    statements (the prompts are in English); the user's values stay verbatim.
    Empty fields are skipped — nothing is invented. "current or past" tells the
    model a value may be OLD (so it recognises documents from a former employer
    or provider). `[Section]` labels are kept by the context loader (only `#`
    lines and HTML comments are stripped)."""
    lines: list[str] = []

    me: list[str] = []
    name = " ".join(p for p in (a.get("first_name"), a.get("last_name")) if p)
    if name:
        me.append(f"My name is {name}.")
    _line(me, "Online aliases that also mean me", a.get("aliases"))
    if me:
        lines += ["[About me]", *me, ""]

    work: list[str] = []
    _line(work, "Professions (current or past)", a.get("professions"))
    _line(work, "Employers (current or past)", a.get("employers"))
    _line(work, "Own businesses / activities (current or past)", a.get("businesses"))
    _line(work, "Names that mean my work", a.get("work_names"))
    if work:
        lines += ["[Work — what is professional for me]", *work, ""]

    if a.get("interests"):
        lines += ["[My interests]", "My hobbies / interests: " + ", ".join(a["interests"]) + ".", ""]

    if a.get("online_content"):
        lines += [
            "[Online content]",
            "I create or publish online content on/as: " + ", ".join(a["online_content"]) + ".",
            "",
        ]

    household: list[str] = []
    _line(household, "Banks (current or past)", a.get("banks"))
    _line(household, "Insurers", a.get("insurers"))
    _line(household, "Energy/water providers (current or past)", a.get("energy"))
    _line(household, "Phone/internet providers", a.get("telecom"))
    _line(household, "Rented homes (current or past), by place", a.get("rentals"))
    _line(household, "Owned properties (current or past), by place", a.get("properties"))
    _line(household, "Vehicles", a.get("vehicles"))
    _line(household, "Household members", a.get("household"))
    if household:
        lines += ["[Household]", *household, ""]

    if a.get("notes"):
        lines += ["[Other]", a["notes"], ""]

    return "\n".join(lines).strip() + "\n"


def setup_context(*, ask: AskFn = input, out: OutFn = print) -> Path | None:
    """Run the questionnaire, show a recap, and (on confirm) write the context
    file. Returns the written path, or None if the user cancelled."""
    out("A few questions to file your documents well.")
    out("Enter = skip · several answers = COMMAS (e.g. BNP Paribas, Barclays)")
    out("Everything stays on your machine (never committed, never shared).")
    answers = collect_answers(ask, out)

    rendered = render_context(answers)
    out("\n" + "─" * 56)
    out("Here is what I captured:\n")
    out(rendered)
    out("─" * 56)

    if _choice(ask, out, "Looks good?", ["Save", "Cancel"]) != 0:
        out("Cancelled. Nothing was saved.")
        return None

    target = default_context_write_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    out(f"✓ Context saved: {target}")

    language = answers.get("language")
    if language:
        from procrafiler.config import default_runtime_paths, set_user_language
        try:
            set_user_language(default_runtime_paths(), language)
            out(f"✓ Main language: {language} (multilingual search)")
        except ValueError:
            pass

    active = active_context_path()
    if active is not None and active.resolve() != target.resolve():
        out(
            f"⚠ Warning: the app will read {active} first (higher priority). "
            "Move/remove that file, or point PROCRAFILER_CONTEXT_FILE at the right one."
        )
    out("Run it again any time: procrafiler setup-context")
    return target
