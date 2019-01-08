import os
import openpyxl
import tempfile
from collections import defaultdict
from decimal import Decimal
from sql import Column
from sql.aggregate import Sum
from sql.conditionals import Coalesce
from trytond.report import Report
from trytond.model import ModelSQL, ModelView, MatchMixin, fields
from trytond.transaction import Transaction
from trytond.pool import Pool

__all__ = ['AnalyticDistributionReport', 'AnalyticDistributionReportRule',
    'SpreadsheetReport']
_ZERO = Decimal(0)


class AnalyticDistributionReport(ModelSQL, ModelView):
    'Analytic Distribution Report'
    __name__ = 'analytic.distribution.report'
    name = fields.Char('Name', required=True)
    company = fields.Many2One('company.company', 'Company', required=True)
    start_date = fields.Date('Start Date', required=True)
    end_date = fields.Date('End Date', required=True)
    rules = fields.One2Many('analytic.distribution.report.rule', 'report',
        'Rules')

    @classmethod
    def __setup__(cls):
        super(AnalyticDistributionReport, cls).__setup__()
        cls._error_messages.update({
                'account_in_source_and_target': ('Analytic Account '
                    '"%(account)s" cannot be configured as source and target '
                    'in report "%(report)s".'),
                })

    @staticmethod
    def default_company():
        return Transaction().context.get('company')

    @classmethod
    def validate(cls, reports):
        for report in reports:
            report.check_source_target()

    def check_source_target(self):
        targets = set([])
        for rule in self.rules:
            targets.add(rule.target_analytic_account)
            if rule.source_analytic_account in targets:
                self.raise_user_error('account_in_source_and_target', {
                        'account': rule.source_analytic_account.rec_name,
                        'report': self.rec_name,
                        })

    def spread(self, analytic, amount):
        res = {}
        total = _ZERO
        sign = 1 if amount >= 0 else -1
        amount = abs(amount)
        for rule in self.rules:
            if rule.source_analytic_account == analytic:
                spread_amount = amount * Decimal(str(rule.ratio))
                total += spread_amount
                res[rule.target_analytic_account.id] = spread_amount
                last = rule.target_analytic_account
        if res and total != amount:
            if not last.id in res:
                res[last.id] = _ZERO
            res[last.id] += total - amount
        elif not res:
            res[analytic.id] = amount

        for k, v in res.iteritems():
            res[k] *= sign
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
            print
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
            for k, v in spread.iteritems():
                key = (k, account_id)
                if key not in result:
                    result[key] = _ZERO
                result[key] += v

        # Generate .XLSX
        wb = openpyxl.Workbook(write_only=True)
        ws = wb.create_sheet()

        analytics = Analytic.search([
                ('type', '=', 'normal'),
                ])
        row = ['']
        for analytic in analytics:
            row.append(analytic.rec_name)
        ws.append(row)
        for account in Account.search([
                    ('kind', 'in', ('expense', 'revenue')),
                    ], order=[('code', 'ASC'), ('name', 'ASC')]):
            to_add = False
            row = [account.rec_name]
            for analytic in analytics:
                key = (analytic.id, account.id)
                value = result.get(key, _ZERO)
                if value:
                    to_add = True
                row.append(value)
            if to_add:
                ws.append(row)

        fd, filename = tempfile.mkstemp()
        try:
            os.close(fd)
            wb.save(filename)
            with open(filename, 'r') as f:
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


class AnalyticDistributionReportRule(ModelSQL, ModelView, MatchMixin):
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
