import dataclasses


@dataclasses.dataclass
class Facet:
    title: str
    href: str
    group: str
    count: int
    is_active: bool = False
