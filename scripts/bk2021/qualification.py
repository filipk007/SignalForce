"""Industrial battery procurement qualification from the published order items."""

import re

from scripts.bk2021.models import Announcement

BATTERY = re.compile(
    r"\bbess\b|\bmagazyn\w*\s+(?:(?:energii\s+elektrycznej|energii)(?!\w)|"
    r"bateryjn\w*)|\b(?:system\w*\s+)?magazynowani\w*\s+energii\b",
    re.IGNORECASE,
)
BUY = re.compile(
    r"\b(?:zakup\w*|dostaw\w*|dostarczeni\w*|montaż\w*|budow\w*|"
    r"wybudow\w*|zamontowa\w*|wdrożeni\w*|wykonani\w*|zabudow\w*|"
    r"zaprojektuj\s+i\s+wybuduj)\b",
    re.IGNORECASE,
)
OTHER_SCOPE = re.compile(
    r"\b(?:audyt\w*|nadz[oó]r\w*|nadzor\w*|inspektor\w*|ekspertyz\w*|studium|"
    r"usług\w*\s+doradcz\w*|serwis\w*|przegląd\w*|dokumentacj\w*\s+projektow\w*|"
    r"opracowani\w*\s+(?:koncepcj\w*|projekt\w*)|projekt\w*\s+budow\w*|"
    r"możliwoś\w*|opcj\w*|w\s+przyszłości|istniejąc\w*|kompatybiln\w*|z\s+wykorzystani\w*)\b",
    re.IGNORECASE,
)
NON_INDUSTRIAL = re.compile(
    r"\b(?:przydomow\w*|gospodarstw\w*\s+domow\w*|budyn\w*\s+mieszkaln\w*|"
    r"dom\w*\s+jednorodzinn\w*|mieszkańc\w*\s+(?:gmin\w*|miast\w*)|"
    r"prosumen(?:t|c)\w*|sieciow\w*\s+magazyn\w*|magazyn\w*\s+sieciow\w*|"
    r"(?:na\s+potrzeby|dla)\s+(?:KSE|sieci\s+(?:przesyłow\w*|dystrybucyjn\w*)|"
    r"operator\w*\s+systemu)|(?:stabilizacj\w*|bilansowani\w*)\s+sieci|"
    r"(?:standalone|utility[- ]scale)\b)",
    re.IGNORECASE,
)
NON_BATTERY = re.compile(
    r"\b(?:wodorow\w*|pneumatyczn\w*|grawitacyjn\w*)\s+magazyn\w*\s+energii\b"
    r"|\b(?:magazyn\w*|magazynowani\w*)\s+(?:ciepła|chłodu|energii\s+"
    r"(?:ciepln\w*|termiczn\w*|wodorow\w*|grawitacyjn\w*))\b",
    re.IGNORECASE,
)
BUSINESS = re.compile(
    r"\b(?:przemysłow\w*|komercyjn\w*|zakład\w*\s+produkcyjn\w*|"
    r"hal\w*\s+(?:produkcyjn|przemysłow)\w*|fabryk\w*|"
    r"(?:dla|potrzeb\w*|terenie|siedzib\w*)\s+(?:zakład\w*|przedsiębiorstw\w*))\b",
    re.IGNORECASE,
)
SERVICE_TITLE = re.compile(
    r"^\W*(?:(?:usług\w*|wykonani\w*|pełnieni\w*)\s+)?"
    r"(?:audyt\w*|nadz[oó]r\w*|nadzor\w*|inspektor\w*|serwis\w*|przegląd\w*)\b",
    re.IGNORECASE,
)
NOT_INCLUDED = re.compile(
    r"\bnie\s+(?:jest\s+przedmiotem|wchodzi\s+w\s+zakres|obejmuje)\b", re.IGNORECASE
)
CAPACITY = re.compile(r"(\d+(?:[ \u00a0\u202f]\d{3})*(?:[.,]\d+)?)\s*(kWh|MWh)\b", re.IGNORECASE)
CLAUSES = re.compile(r"[\r\n;]+|(?<=[.!?])\s+(?=[A-ZĄĆĘŁŃÓŚŹŻ])")


def procurement_clause(clause: str) -> bool:
    battery = BATTERY.search(clause)
    if not battery or NON_BATTERY.search(clause):
        return False
    scope = clause[: battery.end()]
    tail = clause[battery.end() :]
    if OTHER_SCOPE.search(scope) or NOT_INCLUDED.search(tail[:100]):
        return False
    return bool(BUY.search(scope) or BUY.match(tail.strip(" \t:–-")))


def industrial_capacity(clause: str, minimum: int) -> bool:
    for battery in BATTERY.finditer(clause):
        capacity = CAPACITY.search(clause[battery.end() :])
        if capacity:
            value, unit = capacity.groups()
            number = float(re.sub(r"\s+", "", value).replace(",", "."))
            if number * (1000 if unit.lower() == "mwh" else 1) >= minimum:
                return True
    return False


def item_evidence(announcement: str, order: str, description: str, minimum: int) -> list[str]:
    context = f"{announcement}\n{order}\n"
    if (
        NON_INDUSTRIAL.search(context)
        or SERVICE_TITLE.match(announcement)
        or SERVICE_TITLE.match(order)
    ):
        return []
    order_procures_battery = procurement_clause(order)
    clauses = [
        clause.strip()
        for clause in CLAUSES.split(description)
        if BATTERY.search(clause)
        and not (NON_INDUSTRIAL.search(clause) or NON_BATTERY.search(clause))
    ]
    capacities = [
        clause
        for clause in clauses
        if not OTHER_SCOPE.search(clause) and industrial_capacity(clause, minimum)
    ]
    for clause in sorted(clauses, key=lambda text: text not in capacities):
        if not procurement_clause(clause) and (
            not order_procures_battery or OTHER_SCOPE.search(clause) or NOT_INCLUDED.search(clause)
        ):
            continue
        if BUSINESS.search(context + clause) or clause in capacities:
            return [clause]
        if capacities:
            return [clause, capacities[0]]
    return []


def qualify(announcement: Announcement, minimum_kwh: int) -> tuple[str, ...]:
    """Find quotes showing that the procuring organization is buying industrial BESS."""
    if not announcement.buyer:
        return ()
    return tuple(
        dict.fromkeys(
            quote
            for order in announcement.orders
            for item in order.items
            for quote in item_evidence(
                announcement.title, order.title, item.description, minimum_kwh
            )
        )
    )
