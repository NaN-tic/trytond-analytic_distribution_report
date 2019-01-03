# This file is part analytic_distribution_report module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
from trytond.pool import Pool
from . import analytic

def register():
    Pool.register(
        analytic.AnalyticDistributionReport,
        analytic.AnalyticDistributionReportRule,
        module='analytic_distribution_report', type_='model')
    Pool.register(
        analytic.SpreadsheetReport,
        module='analytic_distribution_report', type_='report')
