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
    "Musique",
    "Sport",
    "Lecture",
    "Cuisine",
    "Jardinage",
    "Photo",
    "Jeux vidéo",
    "Bricolage",
    "Voyages",
)

_STATUS: tuple[tuple[str, str], ...] = (
    ("salaried", "Salarié·e"),
    ("self", "Indépendant·e / ma propre activité"),
    ("both", "Les deux"),
    ("none", "Aucun (étudiant·e, retraité·e, sans emploi)"),
)


def _text(ask: AskFn, out: OutFn, label: str) -> str:
    out(label)
    return ask("› ").strip()


def _csv(ask: AskFn, out: OutFn, label: str) -> list[str]:
    out(label)
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
    raw = ask("Numéros et/ou mots libres, séparés par des virgules › ")
    selected: list[str] = []
    for token in raw.split(","):
        t = token.strip()
        if not t:
            continue
        val = options[int(t) - 1] if (t.isdigit() and 1 <= int(t) <= len(options)) else t
        if val not in selected:
            selected.append(val)
    return selected


def collect_answers(ask: AskFn, out: OutFn) -> dict[str, Any]:
    """Run the guided questionnaire and return the raw answers (work questions
    branch on the declared status; everything is skippable with Entrée)."""
    a: dict[str, Any] = {}

    out("\n1/5 · Toi")
    a["first_name"] = _text(ask, out, "Prénom ?")
    a["last_name"] = _text(ask, out, "Nom de famille ?")
    a["aliases"] = _csv(ask, out, "Pseudo(s) en ligne ? (te reconnaître dans captures, exports, messages)")

    out("\n2/5 · Ton travail")
    idx = _choice(ask, out, "Ton statut ?", [label for _, label in _STATUS])
    status = _STATUS[idx][0] if idx is not None else "none"
    a["work_status"] = status
    if status != "none":
        a["profession"] = _text(ask, out, "Ton métier ?")
        if status in ("salaried", "both"):
            a["employer"] = _text(ask, out, "Ton employeur ? (ses documents = Travail)")
        if status in ("self", "both"):
            a["business"] = _text(ask, out, "Le nom de ton activité ?")
        a["work_names"] = _csv(
            ask, out, "Des noms liés à ton travail ? (employeur, clients, projets, outils)"
        )

    out("\n3/5 · Tes centres d'intérêt   (crée seulement ces dossiers-là)")
    a["interests"] = _checklist(ask, out, INTERESTS)

    out("\n4/5 · Ton foyer   (optionnel — Entrée pour passer)")
    a["bank"] = _text(ask, out, "Banque ?")
    a["insurer"] = _text(ask, out, "Assurance ?")
    a["energy"] = _csv(ask, out, "Énergie / eau ?")
    a["telecom"] = _text(ask, out, "Téléphone / box ?")
    h = _choice(ask, out, "Logement ?", ["Propriétaire", "Locataire"])
    a["housing"] = ("owner", "renter")[h] if h is not None else ""
    v = _choice(ask, out, "Véhicule ?", ["Oui", "Non"])
    a["vehicle"] = (True, False)[v] if v is not None else None
    a["household"] = _csv(ask, out, "Prénoms du foyer (conjoint, enfants) ?")

    out("\n5/5 · Autre chose à savoir ?")
    a["notes"] = _text(ask, out, "(une phrase, ou Entrée)")
    return a


_STATUS_EN = {
    "salaried": "I am an employee.",
    "self": "I am self-employed.",
    "both": "I am both an employee and self-employed.",
    "none": "I have no professional activity (student / retired / not employed).",
}


def render_context(a: dict[str, Any]) -> str:
    """Render the answers into the plain-text context the AI reads. English
    statements (the prompts are in English); the user's values stay verbatim.
    Empty fields are skipped — nothing is invented. `[Section]` labels are kept
    by the context loader (only `#` lines and HTML comments are stripped)."""
    lines: list[str] = []

    me: list[str] = []
    name = " ".join(p for p in (a.get("first_name"), a.get("last_name")) if p)
    if name:
        me.append(f"My name is {name}.")
    if a.get("aliases"):
        me.append("Online aliases that also mean me: " + ", ".join(a["aliases"]) + ".")
    if me:
        lines += ["[About me]", *me, ""]

    work: list[str] = [_STATUS_EN.get(a.get("work_status", "none"), "")]
    if a.get("profession"):
        work.append(f"Profession: {a['profession']}.")
    if a.get("employer"):
        work.append(f"Employer: {a['employer']}.")
    if a.get("business"):
        work.append(f"My business: {a['business']}.")
    if a.get("work_names"):
        work.append("Names that mean my work: " + ", ".join(a["work_names"]) + ".")
    work = [w for w in work if w]
    if work:
        lines += ["[Work — what is professional for me]", *work, ""]

    if a.get("interests"):
        lines += ["[My interests]", "My hobbies / interests: " + ", ".join(a["interests"]) + ".", ""]

    household: list[str] = []
    if a.get("bank"):
        household.append(f"Bank: {a['bank']}.")
    if a.get("insurer"):
        household.append(f"Insurer: {a['insurer']}.")
    if a.get("energy"):
        household.append("Energy/water providers: " + ", ".join(a["energy"]) + ".")
    if a.get("telecom"):
        household.append(f"Phone/internet: {a['telecom']}.")
    if a.get("housing") == "owner":
        household.append("I own my home.")
    elif a.get("housing") == "renter":
        household.append("I rent my home.")
    if a.get("vehicle") is True:
        household.append("I own a vehicle.")
    if a.get("household"):
        household.append("Household members: " + ", ".join(a["household"]) + ".")
    if household:
        lines += ["[Household]", *household, ""]

    if a.get("notes"):
        lines += ["[Other]", a["notes"], ""]

    return "\n".join(lines).strip() + "\n"


def setup_context(*, ask: AskFn = input, out: OutFn = print) -> Path | None:
    """Run the questionnaire, show a recap, and (on confirm) write the context
    file. Returns the written path, or None if the user cancelled."""
    out("Quelques questions pour bien ranger tes fichiers.")
    out("Entrée = passer · tout reste sur ta machine (jamais committé, jamais partagé)")
    answers = collect_answers(ask, out)

    rendered = render_context(answers)
    out("\n" + "─" * 56)
    out("Voici ce que j'ai retenu :\n")
    out(rendered)
    out("─" * 56)

    if _choice(ask, out, "C'est bon ?", ["Enregistrer", "Annuler"]) != 0:
        out("Annulé. Rien n'a été enregistré.")
        return None

    target = default_context_write_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    out(f"✓ Contexte enregistré : {target}")

    active = active_context_path()
    if active is not None and active.resolve() != target.resolve():
        out(
            f"⚠ Attention : l'app lira d'abord {active} (priorité plus haute). "
            "Déplace/supprime ce fichier, ou pointe PROCRAFILER_CONTEXT_FILE vers le bon."
        )
    out("Le refaire quand tu veux : procrafiler setup-context")
    return target
