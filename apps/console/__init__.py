"""IP-035: the ELVIRA administration console (Django admin, grouped into switchable modules).

Apps keep their `ModelAdmin`s in their own `admin.py` and register them with `apps.console.options.register`,
naming the functional module they belong to. The modules themselves, their defaults and how they are
switched on and off live in `apps.console.modules`; the site that groups and gates them in
`apps.console.site`; the interactive pages (data grid, graph, vectors, jobs) in `apps.console.views`.
"""
