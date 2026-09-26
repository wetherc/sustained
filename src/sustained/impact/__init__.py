"""
Statement impact analysis: what a migration statement does to a live
database while it runs, such as the locks it takes, what those locks
block, and whether the table is rewritten.

The package is being built in phases. This release holds the groundwork:
the shared tokenizer (`sustained.impact.tokens`) and the report model
(`sustained.impact.model`).
"""
