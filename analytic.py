import os
from openpyxl import Workbook
from openpyxl.cell.cell import WriteOnlyCell
import tempfile
from collections import defaultdict
from decimal import Decimal
from sql import Column
from sql.aggregate import Sum
from sql.conditionals import Coalesce
from trytond.report import Report
from trytond.model import (ModelSQL, ModelView, MatchMixin, fields,
    sequence_ordered)
from trytond.transaction import Transaction
from trytond.pool import Pool
from trytond.exceptions import UserError
from trytond.i18n import gettext

__all__ = ['AnalyticDistributionReport', 'AnalyticDistributionReportRule',
    'SpreadsheetReport']
_ZERO = Decimal(0)
_FORMAT = '#,###,###,##0.00'


def round(number, digits=2):
    quantize = Decimal(10) ** -Decimal(digits)
    return Decimal(number).quantize(quantize)


class AnalyticDistributionReport(ModelSQL, ModelView):
    'Analytic Distribution Report'
    __name__ = 'analytic.distribution.report'
    name = fields.Char('Name', required=True)
    company = fields.Many2One('company.company', 'Company', required=True)
    start_date = fields.Date('Start Date', required=True)
    end_date = fields.Date('End Date', required=True)
    rules = fields.One2Many('analytic.distribution.report.rule', 'report',
        'Rules')

    @staticmethod
    def default_company():
        return Transaction().context.get('company')

    @classmethod
    def validate(cls, reports):
        for report in reports:
            report.check_source_target()

    def check_source_target(self):
        sources = set([])
        for rule in self.rules:
            sources.add(rule.source_analytic_account)
            if rule.target_analytic_account in sources:
                raise UserError(gettext('analytic_distribution_report.'
                        'msg_account_in_target_after_source',
                        account=rule.source_analytic_account.rec_name,
                        report=self.rec_name))

    def spread(self, analytic, amount):
        res = {}
        total = _ZERO
        sign = 1 if amount >= 0 else -1
        amount = abs(amount)
        children = set([])
        for rule in self.rules:
            if rule.source_analytic_account == analytic:
                spread_amount = round(amount * Decimal(str(rule.ratio)))
                total += spread_amount
                res[rule.target_analytic_account.id] = spread_amount
                last = rule.target_analytic_account
            elif rule.source_analytic_account.id in res:
                children.add(rule.source_analytic_account)
        if res and total != amount:
            if not last.id in res:
                res[last.id] = _ZERO
            res[last.id] += round(total - amount)
        elif not res:
            res[analytic.id] = amount

        for k, v in res.items():
            res[k] *= sign

        for child in children:
            r = self.spread(child, res[child.id])
            del res[child.id]
            res.update(r)
        return res

    def spreadsheet(self):
        pool = Pool()
        Account = pool.get('account.account')
        Analytic = pool.get('analytic_account.account')
        Line = pool.get('analytic_account.line')
        MoveLine = pool.get('account.move.line')
        Company = pool.get('company.company')
        Currency = pool.get('currency.currency')

        cursor = Transaction().connection.cursor()
        table = Analytic.__table__()
        line = Line.__table__()
        move_line = MoveLine.__table__()
        a_account = Account.__table__()
        company = Company.__table__()

        # Get analytic credit, debit grouped by account.account
        id2account = {}
        for account in Analytic.search([
                    ('type', '=', 'normal'),
                    ]):
            id2account[account.id] = account

        with Transaction().set_context({
                    'start_date': self.start_date,
                    'end_date': self.end_date,
                    }):
            line_query = Line.query_get(line)
        cursor.execute(*table.join(line, 'INNER',
                condition=table.id == line.account
                ).join(move_line, 'LEFT',
                condition=move_line.id == line.move_line
                ).join(a_account, 'LEFT',
                condition=a_account.id == move_line.account
                ).join(company, 'LEFT',
                condition=company.id == a_account.company
                ).select(table.id, move_line.account,
                company.currency,
                Sum(Coalesce(Column(line, 'credit'), 0)) -
                Sum(Coalesce(Column(line, 'debit'), 0)),
                where=table.active & line_query
                & (company.id == self.company.id),
                group_by=(table.id, move_line.account, company.currency)))

        result = {}
        id2currency = {}
        for row in cursor.fetchall():
            analytic = id2account[row[0]]
            account_id = row[1]
            currency_id = row[2]
            balance = row[3]
            # SQLite uses float for SUM
            if not isinstance(balance, Decimal):
                balance = Decimal(str(balance))
            if currency_id and currency_id != analytic.currency.id:
                currency = None
                if currency_id in id2currency:
                    currency = id2currency[currency_id]
                else:
                    currency = Currency(currency_id)
                    id2currency[currency.id] = currency

                balance = Currency.compute(currency, balance,
                    analytic.currency, round=True)
            else:
                balance = analytic.currency.round(balance)

            spread = self.spread(analytic, balance)
            for k, v in spread.items():
                key = (k, account_id)
                if key not in result:
                    result[key] = _ZERO
                result[key] += v

        # Generate .XLSX
        wb = Workbook(write_only=True)
        ws = wb.create_sheet()

        # Add header
        row = [self.name, self.start_date, self.end_date]
        ws.append(row)
        ws.append([])

        # Add data
        analytics = Analytic.search([
                ('type', '=', 'normal'),
                ])
        analytics = [dict(name=x.rec_name, id=x.id) for x in analytics]
        analytics.sort(key=lambda x: x['name'])
        totals = defaultdict(lambda: _ZERO)
        row = [''] + [x['name'] for x in analytics]
        ws.append(row)
        for account in Account.search([
                    ['OR',
                        ('type.expense', '=', True),
                        ('type.revenue', '=', True),
                        ],
                    ], order=[('code', 'ASC'), ('name', 'ASC')]):
            to_add = False
            # Add account name
            row = [account.rec_name]
            amount = _ZERO
            for analytic in analytics:
                key = (analytic['id'], account.id)
                value = result.get(key, _ZERO)
                if value:
                    to_add = True
                    amount += value
                    totals[analytic['id']] += value
                cell = WriteOnlyCell(ws, value)
                cell.number_format = _FORMAT
                row.append(cell)
            # Add row total
            cell = WriteOnlyCell(ws, amount)
            cell.number_format = _FORMAT
            row.append(cell)
            if to_add:
                ws.append(row)

        row = ['']
        for analytic in analytics:
            cell = WriteOnlyCell(ws, totals[analytic['id']])
            cell.number_format = _FORMAT
            row.append(cell)
        ws.append(row)

        fd, filename = tempfile.mkstemp()
        try:
            os.close(fd)
            wb.save(filename)
            with open(filename, 'rb') as f:
                data = f.read()
        finally:
            os.unlink(filename)
        return data


class SpreadsheetReport(Report):
    'Analytic Distribution Report Spreadsheet'
    __name__ = 'analytic.distribution.report.spreadsheet'

    @classmethod
    def execute(cls, ids, data):
        pool = Pool()
        ActionReport = pool.get('ir.action.report')
        Report = pool.get('analytic.distribution.report')
        cls.check_access()

        report = Report(ids[0])
        content = report.spreadsheet()

        action_id = data.get('action_id')
        if action_id is None:
            action_reports = ActionReport.search([
                    ('report_name', '=', cls.__name__)
                    ])
            assert action_reports, '%s not found' % cls
            action_report = action_reports[0]
        else:
            action_report = ActionReport(action_id)

        return ('xlsx', bytearray(content), False, action_report.name)


class AnalyticDistributionReportRule(sequence_ordered(), ModelSQL, ModelView,
    MatchMixin):
    'Analytic Distribution Report Rule'
    __name__ = 'analytic.distribution.report.rule'
    report = fields.Many2One('analytic.distribution.report', 'Report',
        required=True, ondelete='CASCADE')
    source_analytic_account = fields.Many2One('analytic_account.account',
        'Source Analytic Account', required=True, ondelete='CASCADE')
    target_analytic_account = fields.Many2One('analytic_account.account',
        'Target Analytic Account', required=True, ondelete='CASCADE')
    amount = fields.Numeric('Amount', required=True)
    ratio = fields.Function(fields.Float('Ratio', digits=(16, 4)), 'get_ratio')

    @classmethod
    def get_ratio(cls, rules, name):
        report_ids = set([x.report for x in rules])

        amounts = dict([(x, defaultdict(lambda: _ZERO)) for x in report_ids])
        for rule in cls.search([('report', 'in', report_ids)]):
            if not rule.amount:
                continue
            amounts[rule.report][rule.source_analytic_account] += rule.amount

        res = {}
        for rule in rules:
            total = amounts[rule.report][rule.source_analytic_account]
            if total:
                res[rule.id] = float(rule.amount / total)
            else:
                res[rule.id] = 0.0
        return res
