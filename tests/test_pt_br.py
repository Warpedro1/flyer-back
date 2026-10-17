"""Everything the user sees is Brazilian Portuguese (decision 2026-10-17).

Messages the API returns, LLM prompts (their output reaches the chat) and the e-mail
templates are pt-BR: "você", 3rd-person imperatives ("Tente", "Aguarde"), "Enviamos",
"tela", "ingresso", "salvar"… This guard looks for the European Portuguese forms
that slipped in. It reads only string literals (not comments or docstrings, which
are for developers) and the files in `supabase/templates/`.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PT_PT = [
    # "tu" and its forms; pt-BR uses "você", "seu/sua".
    r"\b(tu|ti|teu|tua|teus|tuas|contigo|tens|estás|podes|queres|és|foste|fizeste|enviaste|criaste|pediste)\b",
    # Imperatives in the "tu" form (pt-BR: Tente, Aguarde, Verifique, Entre…).
    r"\b(Tenta|Espera|Verifica|Entra|Pede|Usa|Escolhe|Indica|Abre|Faz|Muda|Ignora|Cola|Extrai|Marca|Recebes)\b",
    # "Enviámos", "encontrámos": pt-BR writes the past tense without the accent.
    r"\w+ámos\b",
    # "A carregar…": pt-BR uses the gerund ("Carregando…").
    r"\bA (carregar|guardar|enviar|confirmar|ativar|criar|entrar|validar|verificar|obter|procurar|mostrar|redirecionar)\b",
    # Enclitic pronouns after the verb ("Abre-o", "diz-nos").
    r"\b\w+[aei]-(te|nos|o|a|lo|la)\b",
    # Vocabulary.
    (
        r"\b(ecrã|utilizador\w*|ficheiro\w*|bilhete\w*|câmara|gerir|partilh\w*|ligação|registar|registo|"
        r"canónic\w*|factos?|actual|coleccion\w*|telemóvel|equipa|aceite)\b"
    ),
    r"correu mal|em curso|\btem de\b|guardar o|desligad\w*",
    r"\b(entra|tenta) (outra vez|de novo|novamente)\b",
]
# The imperatives are capitalised words; matching them case-insensitively would flag
# pt-BR third-person verbs in the middle of a sentence ("ele usa", "abre às 9h").
CASE_SENSITIVE = re.compile(PT_PT[1])
INSENSITIVE = re.compile("|".join(f"(?:{p})" for i, p in enumerate(PT_PT) if i != 1), re.IGNORECASE)


def _offences(text: str) -> list[str]:
    found = [m.group(0) for m in INSENSITIVE.finditer(text)]
    found += [m.group(0) for m in CASE_SENSITIVE.finditer(text)]
    return found


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _python_strings() -> list[tuple[str, int, str]]:
    out: list[tuple[str, int, str]] = []
    for path in sorted((ROOT / "app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = _docstring_ids(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
                out.append((str(path.relative_to(ROOT)), node.lineno, node.value))
    return out


def test_the_guard_reads_the_app() -> None:
    assert len(_python_strings()) > 300


def test_the_guard_catches_european_portuguese() -> None:
    for text in (
        "Sessão inválida: entra outra vez.",
        "Enviámos um código.",
        "A carregar…",
        "Abre-o no navegador",
        "Guarda este ecrã",
        "Algo correu mal",
        "Usa este código para ativar a tua conta",
    ):
        assert _offences(text), text
    for text in (
        "Sessão inválida: entre novamente.",
        "Enviamos um código.",
        "Carregando…",
        "Use este código para ativar sua conta",
        "O check-in abre às 20h.",
        "e-mail",
    ):
        assert not _offences(text), text


def test_api_messages_and_prompts_are_pt_br() -> None:
    bad = [
        f"{path}:{line}: {text!r} → {_offences(text)}"
        for path, line, text in _python_strings()
        if _offences(text)
    ]
    assert bad == []


def test_email_templates_are_pt_br() -> None:
    bad = []
    for path in sorted((ROOT / "supabase" / "templates").iterdir()):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _offences(line):
                bad.append(f"{path.name}:{n}: {line.strip()} → {_offences(line)}")
    assert bad == []
