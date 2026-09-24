"""
Tests for the check that tells a widening column type change from one
that can lose data.
"""

import unittest

from sustained.dialects import Dialects
from sustained.schema import (
    BigInteger,
    Binary,
    Boolean,
    Date,
    Enum,
    Float,
    Integer,
    Json,
    Numeric,
    String,
    Text,
    Timestamp,
)
from sustained.type_changes import removed_enum_values, type_change_loses_data


def loses(dialect, coldef, raw_type):
    compiler = Dialects.get_compiler(dialect)
    rendered = compiler.compile_column_type(coldef)
    return type_change_loses_data(compiler, coldef, rendered, raw_type)


class TestIntegers(unittest.TestCase):
    def test_a_wider_integer_keeps_the_data(self):
        self.assertFalse(loses(Dialects.POSTGRES, BigInteger(), "integer"))
        self.assertFalse(loses(Dialects.MYSQL, Integer(), "smallint"))
        self.assertFalse(loses(Dialects.MYSQL, BigInteger(), "int unsigned"))

    def test_a_narrower_integer_loses_data(self):
        self.assertTrue(loses(Dialects.MYSQL, Integer(), "bigint"))
        self.assertTrue(loses(Dialects.MYSQL, Integer(), "int unsigned"))
        self.assertTrue(loses(Dialects.MYSQL, BigInteger(), "bigint unsigned"))

    def test_a_fraction_is_lost_to_an_integer(self):
        self.assertTrue(loses(Dialects.DUCKDB, Integer(), "DOUBLE"))
        self.assertTrue(loses(Dialects.POSTGRES, Integer(), "numeric(10,0)"))

    def test_a_mysql_boolean_is_not_an_integer(self):
        self.assertTrue(loses(Dialects.MYSQL, Integer(), "tinyint(1)"))


class TestDecimals(unittest.TestCase):
    def test_more_digits_on_both_sides_keep_the_data(self):
        self.assertFalse(loses(Dialects.POSTGRES, Numeric(18, 4), "numeric(10,2)"))

    def test_a_smaller_scale_rounds(self):
        self.assertTrue(loses(Dialects.POSTGRES, Numeric(18, 2), "numeric(18,6)"))

    def test_fewer_integer_digits_overflow(self):
        self.assertTrue(loses(Dialects.POSTGRES, Numeric(10, 4), "numeric(10,2)"))

    def test_a_live_decimal_without_scale_reads_as_scale_zero(self):
        self.assertFalse(loses(Dialects.DUCKDB, Numeric(12, 2), "DECIMAL(10)"))

    def test_a_live_decimal_without_precision_loses_data(self):
        self.assertTrue(loses(Dialects.POSTGRES, Numeric(18, 2), "numeric"))

    def test_an_integer_fits_when_the_integer_digits_suffice(self):
        self.assertFalse(loses(Dialects.MYSQL, Numeric(12, 2), "int"))
        self.assertTrue(loses(Dialects.MYSQL, Numeric(12, 2), "bigint"))
        self.assertTrue(loses(Dialects.MYSQL, Numeric(21, 2), "bigint unsigned"))
        self.assertFalse(loses(Dialects.MYSQL, Numeric(22, 2), "bigint unsigned"))

    def test_a_model_numeric_without_precision_counts_as_lossy(self):
        coldef = Numeric(18, 2)
        coldef.precision = None
        self.assertTrue(loses(Dialects.POSTGRES, coldef, "numeric(10,2)"))

    def test_a_float_is_not_a_decimal(self):
        self.assertTrue(loses(Dialects.POSTGRES, Numeric(18, 6), "double precision"))


class TestFloats(unittest.TestCase):
    def test_a_single_float_widens_to_double(self):
        self.assertFalse(loses(Dialects.POSTGRES, Float(), "real"))

    def test_an_int_converts_exactly(self):
        self.assertFalse(loses(Dialects.DUCKDB, Float(), "INTEGER"))

    def test_a_bigint_or_decimal_loses_precision(self):
        self.assertTrue(loses(Dialects.DUCKDB, Float(), "BIGINT"))
        self.assertTrue(loses(Dialects.DUCKDB, Float(), "DECIMAL(18,6)"))


class TestStrings(unittest.TestCase):
    def test_a_longer_varchar_keeps_the_data(self):
        self.assertFalse(loses(Dialects.POSTGRES, String(100), "character varying(50)"))

    def test_a_shorter_varchar_truncates(self):
        self.assertTrue(loses(Dialects.MYSQL, String(10), "varchar(50)"))

    def test_text_to_varchar_truncates(self):
        self.assertTrue(loses(Dialects.MYSQL, String(10), "text"))

    def test_a_varchar_with_no_reported_length_may_not_fit(self):
        self.assertTrue(loses(Dialects.MSSQL, String(50), "nvarchar"))

    def test_mssql_nvarchar_without_a_length_holds_one_character(self):
        coldef = String(10)
        coldef.length = None
        self.assertTrue(loses(Dialects.MSSQL, coldef, "nvarchar(1)"))

    def test_a_varchar_without_a_length_is_unbounded_elsewhere(self):
        coldef = String(10)
        coldef.length = None
        self.assertFalse(loses(Dialects.POSTGRES, coldef, "text"))

    def test_an_enum_fits_a_varchar_as_long_as_its_longest_value(self):
        self.assertFalse(loses(Dialects.MYSQL, String(6), "enum('open','closed')"))
        self.assertTrue(loses(Dialects.MYSQL, String(5), "enum('open','closed')"))

    def test_any_string_fits_text(self):
        self.assertFalse(loses(Dialects.MYSQL, Text(), "varchar(255)"))
        self.assertFalse(loses(Dialects.MYSQL, Text(), "tinytext"))
        self.assertFalse(loses(Dialects.MSSQL, Text(), "nvarchar"))

    def test_mysql_large_text_does_not_fit_text(self):
        self.assertTrue(loses(Dialects.MYSQL, Text(), "longtext"))
        self.assertTrue(loses(Dialects.MYSQL, Text(), "mediumtext"))

    def test_a_number_is_not_a_string(self):
        self.assertTrue(loses(Dialects.POSTGRES, Text(), "integer"))


class TestEnums(unittest.TestCase):
    def test_an_inline_enum_that_adds_values_keeps_the_data(self):
        coldef = Enum("open", "closed", "held", name="status")
        self.assertFalse(loses(Dialects.MYSQL, coldef, "enum('open','closed')"))

    def test_an_inline_enum_over_a_string_column_counts_as_lossy(self):
        coldef = Enum("open", "closed", name="status")
        self.assertTrue(loses(Dialects.MYSQL, coldef, "varchar(10)"))

    def test_an_inline_enum_that_removes_values_counts_as_lossy(self):
        coldef = Enum("open", name="status")
        self.assertTrue(loses(Dialects.MYSQL, coldef, "enum('open','closed')"))

    def test_a_check_enum_is_a_varchar_of_its_longest_value(self):
        coldef = Enum("open", "closed", name="status")
        self.assertFalse(loses(Dialects.MSSQL, coldef, "nvarchar(4)"))
        self.assertTrue(loses(Dialects.MSSQL, coldef, "nvarchar(20)"))

    def test_a_native_enum_counts_as_lossy(self):
        coldef = Enum("open", "closed", name="status")
        self.assertTrue(loses(Dialects.POSTGRES, coldef, "character varying(10)"))

    def test_removed_values_come_back_in_column_order(self):
        coldef = Enum("closed", name="status")
        self.assertEqual(
            removed_enum_values(coldef, "enum('open','closed','held')"),
            ("open", "held"),
        )
        self.assertEqual(removed_enum_values(coldef, "varchar(10)"), ())


class TestOtherTypes(unittest.TestCase):
    def test_a_boolean_takes_only_a_boolean(self):
        self.assertFalse(loses(Dialects.MSSQL, Boolean(), "bit"))
        self.assertFalse(loses(Dialects.POSTGRES, Boolean(), "tinyint(1)"))
        self.assertTrue(loses(Dialects.POSTGRES, Boolean(), "integer"))

    def test_a_timestamp_to_date_loses_the_time(self):
        self.assertTrue(loses(Dialects.POSTGRES, Date(), "timestamp without time zone"))
        self.assertFalse(loses(Dialects.POSTGRES, Date(), "date"))

    def test_a_date_or_timestamp_fits_a_timestamp(self):
        self.assertFalse(loses(Dialects.POSTGRES, Timestamp(), "date"))
        self.assertFalse(loses(Dialects.MYSQL, Timestamp(), "timestamp"))
        self.assertFalse(loses(Dialects.MSSQL, Timestamp(), "datetime"))

    def test_a_time_zone_is_lost_to_a_plain_timestamp(self):
        self.assertTrue(
            loses(Dialects.POSTGRES, Timestamp(), "timestamp with time zone")
        )
        self.assertTrue(
            loses(Dialects.POSTGRES, Timestamp(), "timestamp(3) with time zone")
        )
        self.assertTrue(loses(Dialects.POSTGRES, Timestamp(), "timestamptz"))
        self.assertTrue(loses(Dialects.MSSQL, Timestamp(), "datetimeoffset"))

    def test_a_reported_precision_may_be_finer_than_the_default(self):
        self.assertTrue(loses(Dialects.MYSQL, Timestamp(), "datetime(6)"))

    def test_binary_kinds_fit_a_binary(self):
        self.assertFalse(loses(Dialects.POSTGRES, Binary(), "bytea"))
        self.assertFalse(loses(Dialects.MYSQL, Binary(), "varbinary(16)"))
        self.assertTrue(loses(Dialects.MYSQL, Binary(), "longblob"))
        self.assertTrue(loses(Dialects.MYSQL, Binary(), "text"))

    def test_json_takes_only_json(self):
        self.assertFalse(loses(Dialects.POSTGRES, Json(), "json"))
        self.assertTrue(loses(Dialects.POSTGRES, Json(), "text"))

    def test_mssql_json_is_text(self):
        self.assertFalse(loses(Dialects.MSSQL, Json(), "nvarchar"))

    def test_an_unknown_model_type_counts_as_lossy(self):
        coldef = Integer()
        coldef.type_name = "INTERVAL"
        compiler = Dialects.get_compiler(Dialects.POSTGRES)
        self.assertTrue(type_change_loses_data(compiler, coldef, "INTERVAL", "integer"))

    def test_a_parameter_that_is_not_a_number_is_left_out(self):
        self.assertTrue(loses(Dialects.MSSQL, String(50), "nvarchar(max)"))


if __name__ == "__main__":
    unittest.main()
