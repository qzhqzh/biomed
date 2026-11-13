"""Fixed JSON paths shared by identifier queries and their SQLite indexes."""

from django.db.models import CharField, Func


class LowerIdentifier(Func):
    output_field = CharField()
    template = "LOWER(JSON_EXTRACT(%(expressions)s, '$.%(identifier)s'))"

    def __init__(self, identifier):
        if identifier not in ("doi", "pmc"):
            raise ValueError("Unsupported identifier")
        super().__init__("identifiers", identifier=identifier)
