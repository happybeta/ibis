from __future__ import annotations

import functools
from collections.abc import Mapping
from typing import NamedTuple

import toolz

import ibis.common.exceptions as com
import ibis.expr.analysis as an
import ibis.expr.datatypes as dt
import ibis.expr.operations as ops


class _LimitSpec(NamedTuple):
    n: int
    offset: int


def _get_scalar(field):
    def scalar_handler(results):
        return results[field][0]

    return scalar_handler


def _get_column(name):
    def column_handler(results):
        return results[name]

    return column_handler


class SelectBuilder:
    """Transforms expression IR to a query pipeline.

    There will typically be a primary SELECT query, perhaps with some
    subqueries and other DDL to ingest and tear down intermediate data sources.

    Walks the expression tree and catalogues distinct query units,
    builds select statements (and other DDL types, where necessary), and
    records relevant query unit aliases to be used when actually
    generating SQL.
    """

    def to_select(
        self,
        select_class,
        table_set_formatter_class,
        node,
        context,
        translator_class,
    ):
        self.select_class = select_class
        self.table_set_formatter_class = table_set_formatter_class
        self.context = context
        self.translator_class = translator_class

        self.op, self.result_handler = self._adapt_operation(node)
        assert isinstance(self.op, ops.Node), type(self.op)

        self.table_set = None
        self.select_set = None
        self.group_by = None
        self.having = None
        self.filters = []
        self.limit = None
        self.order_by = []
        self.subqueries = []
        self.distinct = False

        select_query = self._build_result_query()

        self.queries = [select_query]

        return select_query

    @staticmethod
    def _adapt_operation(node):
        # Non-table expressions need to be adapted to some well-formed table
        # expression, along with a way to adapt the results to the desired
        # arity (whether array-like or scalar, for example)
        #
        # Canonical case is scalar values or arrays produced by some reductions
        # (simple reductions, or distinct, say)
        if isinstance(node, ops.TableNode):
            return node, toolz.identity

        elif isinstance(node, ops.Value):
            if node.output_shape.is_scalar():
                if an.is_scalar_reduction(node):
                    table_expr = an.reduction_to_aggregation(node)
                    return table_expr.op(), _get_scalar(node.name)
                else:
                    return node, _get_scalar(node.name)
            elif node.output_shape.is_columnar():
                if isinstance(node, ops.TableColumn):
                    table_expr = node.table.to_expr()[[node.name]]
                    result_handler = _get_column(node.name)
                else:
                    table_expr = node.to_expr().as_table()
                    result_handler = _get_column(node.name)

                return table_expr.op(), result_handler
            else:
                raise com.TranslationError(f"Unexpected shape {node.output_shape}")
        else:
            raise com.TranslationError(f'Do not know how to execute: {type(node)}')

    def _build_result_query(self):
        self._collect_elements()
        self._analyze_subqueries()
        self._populate_context()

        return self.select_class(
            self.table_set,
            list(self.select_set),
            translator_class=self.translator_class,
            table_set_formatter_class=self.table_set_formatter_class,
            context=self.context,
            subqueries=self.subqueries,
            where=self.filters,
            group_by=self.group_by,
            having=self.having,
            limit=self.limit,
            order_by=self.order_by,
            distinct=self.distinct,
            result_handler=self.result_handler,
            parent_op=self.op,
        )

    def _populate_context(self):
        # Populate aliases for the distinct relations used to output this
        # select statement.
        if self.table_set is not None:
            self._make_table_aliases(self.table_set)

    # TODO(kszucs): should be rewritten using lin.traverse()
    def _make_table_aliases(self, node):
        ctx = self.context

        if isinstance(node, ops.Join):
            for arg in node.args:
                if isinstance(arg, ops.TableNode):
                    self._make_table_aliases(arg)
        elif not ctx.is_extracted(node):
            ctx.make_alias(node)
        else:
            # The compiler will apply a prefix only if the current context
            # contains two or more table references. So, if we've extracted
            # a subquery into a CTE, we need to propagate that reference
            # down to child contexts so that they aren't missing any refs.
            ctx.set_ref(node, ctx.top_context.get_ref(node))

    # ---------------------------------------------------------------------
    # Analysis of table set

    def _collect_elements(self):
        # If expr is a Value, we must seek out the Tables that it
        # references, build their ASTs, and mark them in our QueryContext

        # For now, we need to make the simplifying assumption that a value
        # expression that is being translated only depends on a single table
        # expression.

        if isinstance(self.op, ops.TableNode):
            self._collect(self.op, toplevel=True)
            assert self.table_set is not None
        else:
            self.select_set = [self.op]

    def _collect(self, op, toplevel=False):
        method = f'_collect_{type(op).__name__}'

        if hasattr(self, method):
            f = getattr(self, method)
            f(op, toplevel=toplevel)
        elif isinstance(op, (ops.PhysicalTable, ops.SQLQueryResult)):
            self._collect_PhysicalTable(op, toplevel=toplevel)
        elif isinstance(op, ops.Join):
            self._collect_Join(op, toplevel=toplevel)
        else:
            raise NotImplementedError(type(op))

    def _collect_Distinct(self, op, toplevel=False):
        if toplevel:
            self.distinct = True

        self._collect(op.table, toplevel=toplevel)

    def _collect_DropNa(self, op, toplevel=False):
        if toplevel:
            if op.subset is None:
                columns = [
                    ops.TableColumn(op.table, name) for name in op.table.schema.names
                ]
            else:
                columns = op.subset
            if columns:
                filters = [
                    functools.reduce(
                        ops.And if op.how == "any" else ops.Or,
                        [ops.NotNull(c) for c in columns],
                    )
                ]
            elif op.how == "all":
                filters = [ops.Literal(False, dtype=dt.bool)]
            else:
                filters = []
            self.table_set = op.table
            self.select_set = [op.table]
            self.filters = filters

    def _collect_FillNa(self, op, toplevel=False):
        if toplevel:
            table = op.table.to_expr()
            if isinstance(op.replacements, Mapping):
                mapping = op.replacements
            else:
                mapping = {
                    name: op.replacements
                    for name, type in table.schema().items()
                    if type.nullable
                }
            new_op = table.mutate(
                [
                    table[name].fillna(value).name(name)
                    for name, value in mapping.items()
                ]
            ).op()
            self._collect(new_op, toplevel=toplevel)

    def _collect_Limit(self, op, toplevel=False):
        if not toplevel:
            return

        n = op.n
        offset = op.offset or 0

        if self.limit is None:
            self.limit = _LimitSpec(n, offset)
        else:
            self.limit = _LimitSpec(
                min(n, self.limit.n),
                offset + self.limit.offset,
            )

        self._collect(op.table, toplevel=toplevel)

    def _collect_Union(self, op, toplevel=False):
        if toplevel:
            self.table_set = op
            self.select_set = [op]

    def _collect_Difference(self, op, toplevel=False):
        if toplevel:
            self.table_set = op
            self.select_set = [op]

    def _collect_Intersection(self, op, toplevel=False):
        if toplevel:
            self.table_set = op
            self.select_set = [op]

    def _collect_Aggregation(self, op, toplevel=False):
        # The select set includes the grouping keys (if any), and these are
        # duplicated in the group_by set. SQL translator can decide how to
        # format these depending on the database. Most likely the
        # GROUP BY 1, 2, ... style
        if toplevel:
            sub_op = an.substitute_parents(op)

            self.group_by = self._convert_group_by(sub_op.by)
            self.having = sub_op.having
            self.select_set = sub_op.by + sub_op.metrics
            self.table_set = sub_op.table
            self.filters = sub_op.predicates
            self.order_by = sub_op.sort_keys

            self._collect(op.table)

    def _collect_Selection(self, op, toplevel=False):
        table = op.table

        if toplevel:
            if isinstance(table, ops.Join):
                self._collect_Join(table)
            else:
                self._collect(table)

            selections = op.selections
            sort_keys = op.sort_keys
            filters = op.predicates

            if not selections:
                # select *
                selections = [table]

            self.order_by = sort_keys
            self.select_set = selections
            self.table_set = table
            self.filters = filters

    def _collect_InMemoryTable(self, node, toplevel=False):
        if toplevel:
            self.select_set = [node]
            self.table_set = node

    def _convert_group_by(self, nodes):
        return list(range(len(nodes)))

    def _collect_Join(self, op, toplevel=False):
        if toplevel:
            subbed = an.substitute_parents(op)
            self.table_set = subbed
            self.select_set = [subbed]

    def _collect_PhysicalTable(self, op, toplevel=False):
        if toplevel:
            self.select_set = [op]
            self.table_set = op

    def _collect_SelfReference(self, op, toplevel=False):
        if toplevel:
            self._collect(op.table, toplevel=toplevel)

    # --------------------------------------------------------------------
    # Subquery analysis / extraction

    def _analyze_subqueries(self):
        # Somewhat temporary place for this. A little bit tricky, because
        # subqueries can be found in many places
        # - With the table set
        # - Inside the where clause (these may be able to place directly, some
        #   cases not)
        # - As support queries inside certain expressions (possibly needing to
        #   be extracted and joined into the table set where they are
        #   used). More complex transformations should probably not occur here,
        #   though.
        #
        # Duplicate subqueries might appear in different parts of the query
        # structure, e.g. beneath two aggregates that are joined together, so
        # we have to walk the entire query structure.
        #
        # The default behavior is to only extract into a WITH clause when a
        # subquery appears multiple times (for DRY reasons). At some point we
        # can implement a more aggressive policy so that subqueries always
        # appear in the WITH part of the SELECT statement, if that's what you
        # want.

        # Find the subqueries, and record them in the passed query context.
        subqueries = an.find_subqueries(
            [self.table_set, *self.filters], min_dependents=2
        )

        self.subqueries = []
        for node in subqueries:
            # See #173. Might have been extracted already in a parent context.
            if not self.context.is_extracted(node):
                self.subqueries.append(node)
                self.context.set_extracted(node)
