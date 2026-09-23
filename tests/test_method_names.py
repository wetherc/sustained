import unittest

from sustained import Model, RelationType
from sustained.builders.join_builder import OnClauseBuilder
from sustained.builders.where_builder import WhereClauseBuilder
from sustained.naming import fold_name, resolve_public_name


class Venue(Model):
    tableName = "venues"


class Show(Model):
    tableName = "shows"
    relationMappings = {
        "venue": {
            "relation": RelationType.BelongsToOneRelation,
            "modelClass": Venue,
            "join": {"from": "shows.venue_id", "to": "venues.id"},
        },
    }


class TestNaming(unittest.TestCase):
    def test_fold_name_drops_case_and_underscores(self):
        self.assertEqual(fold_name("WHERE_IN"), "wherein")
        self.assertEqual(fold_name("whereIn"), "wherein")

    def test_resolve_public_name_returns_the_defined_spelling(self):
        self.assertEqual(resolve_public_name(OnClauseBuilder, "AND_ON"), "andOn")

    def test_resolve_public_name_skips_private_names(self):
        self.assertIsNone(resolve_public_name(OnClauseBuilder, "add_condition"))


class TestQueryBuilderSpellings(unittest.TestCase):
    def assertSameSql(self, build):
        canonical = str(build("canonical"))
        for spelling in ("snake", "upper", "mixed"):
            self.assertEqual(str(build(spelling)), canonical, spelling)

    def test_defined_methods(self):
        names = {
            "canonical": ("select", "limit"),
            "snake": ("select", "limit"),
            "upper": ("SELECT", "LIMIT"),
            "mixed": ("SeLeCt", "LiMiT"),
        }

        def build(spelling):
            select, limit = names[spelling]
            query = getattr(Show.query(), select)("title")
            return getattr(query, limit)(5)

        self.assertSameSql(build)

    def test_defined_snake_case_method(self):
        self.assertEqual(
            str(Show.query().SELECT_FUNC("upper", "title")),
            str(Show.query().select_func("upper", "title")),
        )
        self.assertEqual(
            str(Show.query().selectFunc("upper", "title")),
            str(Show.query().select_func("upper", "title")),
        )

    def test_where_family(self):
        names = {
            "canonical": ("where", "orWhereIn", "andWhereNotNull"),
            "snake": ("where", "or_where_in", "and_where_not_null"),
            "upper": ("WHERE", "OR_WHERE_IN", "ANDWHERENOTNULL"),
            "mixed": ("Where", "OrWhereIN", "and_WhereNotNULL"),
        }

        def build(spelling):
            first, second, third = names[spelling]
            query = getattr(Show.query(), first)("a", "=", 1)
            query = getattr(query, second)("b", [1, 2])
            return getattr(query, third)("c")

        self.assertSameSql(build)

    def test_ilike_spellings(self):
        expected = str(Show.query().whereILike("title", "%a%"))
        for name in ("where_i_like", "where_ilike", "WHEREILIKE"):
            self.assertEqual(
                str(getattr(Show.query(), name)("title", "%a%")), expected, name
            )

    def test_having_family(self):
        expected = str(
            Show.query()
            .groupBy("title")
            .having("COUNT(id)", ">", 1)
            .orHaving("x", "=", 2)
        )
        query = Show.query().GROUP_BY("title").HAVING("COUNT(id)", ">", 1)
        self.assertEqual(str(query.OR_HAVING("x", "=", 2)), expected)

    def test_group_by_and_order_by(self):
        expected = str(Show.query().groupBy("title").orderBy("title"))
        for group_by, order_by in (("GROUPBY", "ORDERBY"), ("group_by", "order_by")):
            query = getattr(Show.query(), group_by)("title")
            self.assertEqual(str(getattr(query, order_by)("title")), expected)

    def test_join_families(self):
        expected = str(
            Show.query()
            .innerJoinRelated("venue")
            .leftJoin("tickets", "tickets.show_id", "=", "shows.id")
        )
        query = (
            Show.query()
            .INNER_JOIN_RELATED("venue")
            .LEFTJOIN("tickets", "tickets.show_id", "=", "shows.id")
        )
        self.assertEqual(str(query), expected)

    def test_on_builder_inside_a_join(self):
        expected = str(
            Show.query().leftJoin(
                "tickets",
                lambda j: j.on("tickets.show_id", "=", "shows.id").andOn(
                    "tickets.sold", "=", "shows.sold_out"
                ),
            )
        )
        query = Show.query().leftJoin(
            "tickets",
            lambda j: j.ON("tickets.show_id", "=", "shows.id").AND_ON(
                "tickets.sold", "=", "shows.sold_out"
            ),
        )
        self.assertEqual(str(query), expected)

    def test_nested_where_group(self):
        expected = str(
            Show.query().where(lambda w: w.where("a", "=", 1).orWhere("b", "=", 2))
        )
        query = Show.query().where(lambda w: w.WHERE("a", "=", 1).OR_WHERE("b", "=", 2))
        self.assertEqual(str(query), expected)

    def test_registered_function_matches_any_case(self):
        self.assertEqual(
            str(Show.query().Upper("title")), "SELECT UPPER(title) FROM shows"
        )

    def test_aggregate_method_wins_over_registered_function(self):
        self.assertEqual(str(Show.query().COUNT()), str(Show.query().count()))

    def test_unknown_names_raise_attribute_error(self):
        for name in ("wherex", "LIMITS", "_private"):
            with self.subTest(name=name), self.assertRaises(AttributeError):
                getattr(Show.query(), name)
        self.assertFalse(hasattr(Show.query(), "nope"))


class TestClauseBuilderSpellings(unittest.TestCase):
    def test_where_clause_builder_rejects_an_unknown_name(self):
        with self.assertRaises(AttributeError):
            WhereClauseBuilder(Show).orNothing

    def test_on_builder_rejects_unknown_and_private_names(self):
        with self.assertRaises(AttributeError):
            OnClauseBuilder().nope
        with self.assertRaises(AttributeError):
            OnClauseBuilder()._conditions_missing


if __name__ == "__main__":
    unittest.main()
