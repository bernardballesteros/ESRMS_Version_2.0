"""CSV and PDF builders for the Analytics report. Pure: they take the computed summary and records.

A report holds two different things, and every heading says which is which:
  * summary figures (KPIs, status and category counts) calculated from EVERY incident reported
    in the period, whatever the record filters or the export limit;
  * incident rows, which follow the table filters and are capped by the export limit.
When the cap cuts the rows short the report says TRUNCATED / PARTIAL at the top, in the section
heading, at the end of the rows and in the file name. It is never described as a full list.
"""
import csv
import io
import re
from datetime import datetime

from app import analytics as an

RECORD_HEADINGS = ["Incident ID", "Category", "Emergency Type", "Status", "Priority", "Latitude", "Longitude",
                   "Location Verified", "Response Time", "Resolution Time", "Reported (PHT)", "Resolved (PHT)", "Archived"]
SUMMARY_SCOPE = "all incidents reported in the period; not limited by record filters or the export limit"
SIGNED_NUMBER = re.compile(r"[+-]\d[\d.,]*%?")


class PdfUnavailable(Exception):
    """fpdf2 is not installed in this environment."""


def export_meta(summary, matching, exported, limit, filters_text):
    """Counts that describe the export itself, kept apart from the summary figures."""
    truncated = exported < matching
    if not matching:
        status = "NO RECORDS - no incident matches the record filters. The summary figures still cover the whole period."
    elif truncated:
        status = (f"TRUNCATED - PARTIAL RECORD LIST. Only the newest {exported:,} of {matching:,} matching incidents are "
                  f"listed (limit {limit:,} rows per export); {matching - exported:,} are NOT included. The summary figures "
                  "are unaffected. Narrow the date range or the filters to export the remaining incidents.")
    else:
        status = f"COMPLETE - all {matching:,} matching incident(s) are listed."
    return {
        "period_total": summary["kpis"]["total"]["value"], "matching": matching, "exported": exported,
        "missing": matching - exported, "limit": limit, "truncated": truncated, "status": status, "filters": filters_text,
    }


def records_title(meta):
    if not meta["matching"]:
        return "Incident records (none match the record filters)"
    if meta["truncated"]:
        return f"Incident records - PARTIAL: newest {meta['exported']:,} of {meta['matching']:,} matching incidents"
    return f"Incident records - complete: {meta['exported']:,} of {meta['matching']:,} matching incidents"


def local_time(iso):
    moment = an.parse_ts(iso)
    return moment.astimezone(an.PHT).strftime("%Y-%m-%d %H:%M:%S") if moment else ""


def _change(value):
    return "n/a (no data in the previous period)" if value is None else f"{value:+.1f}%"


def _pct(value):
    return "n/a" if value is None else f"{value:.1f}%"


def kpi_rows(summary):
    k = summary["kpis"]
    return [
        ["Total incidents", k["total"]["value"], k["total"]["previous"], _change(k["total"]["change"])],
        ["Average resolution time", k["resolution"]["label"].replace("—", "n/a"),
         k["resolution"]["previous_label"].replace("—", "n/a"), _change(k["resolution"]["change"])],
        ["Resolved incidents", k["resolved"]["value"], k["resolved"]["previous"], _change(k["resolved"]["change"])],
        ["Resolution rate (resolved / total reported)", _pct(k["resolved"]["rate"]), "", ""],
        ["Pending incidents (new, acknowledged)", k["pending"]["value"], k["pending"]["previous"], _change(k["pending"]["change"])],
        ["Pending share of total", _pct(k["pending"]["share"]), "", ""],
        ["Critical priority (P1, counted within the statuses)", summary["critical"]["count"], "", ""],
    ]


def header_rows(summary, meta):
    p = summary["period"]
    return [
        ["Reporting period", f"{p['from']} to {p['to']} ({p['label']}, {p['days']} day(s), {p['timezone']})"],
        ["Generated", local_time(summary["generated_at"]) + " PHT"],
        ["Compared with", f"{p['previous']['from']} to {p['previous']['to']}"],
        ["Incidents reported in period (aggregate total)", meta["period_total"]],
        ["Record filters", meta["filters"]],
        ["Incidents matching the record filters", meta["matching"]],
        ["Incident rows in this file", f"{meta['exported']} of {meta['matching']}"],
        ["Export status", meta["status"]],
    ]


def record_row(r):
    located = r["has_location"]
    return [
        r["code"], r["category"], r["emergency_type"], r["status_label"], f"P{r['priority']}" if r["priority"] else "",
        r["latitude"] if located else "", r["longitude"] if located else "",
        ("Yes" if r["location_verified"] else "No") if located else "Location unavailable",
        r["response_label"] if r["response_seconds"] is not None else "",
        r["resolution_label"] if r["resolution_seconds"] is not None else "",
        local_time(r["created_at"]), local_time(r["resolved_at"]) if r["status"] == "resolved" else "",
        "Yes" if r["archived"] else "No",
    ]


def _safe_cell(value):
    """Citizen-typed text must not run as a spreadsheet formula. Plain signed numbers are left alone."""
    text = "" if value is None else str(value)
    risky = text[:1] in ("=", "+", "-", "@", "\t", "\r") and not SIGNED_NUMBER.fullmatch(text)
    return "'" + text if risky else text


def build_csv(summary, records, meta):
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")

    def section(title, headings, rows):
        w.writerow([])
        w.writerow([title])
        w.writerow(headings)
        for row in rows:
            w.writerow([_safe_cell(v) for v in row])

    w.writerow(["ESRMS Analytics Report"])
    for row in header_rows(summary, meta):
        w.writerow([_safe_cell(v) for v in row])
    section(f"KPI summary ({SUMMARY_SCOPE})", ["Metric", "Selected period", "Previous period", "Change"], kpi_rows(summary))
    section("Incidents by status (all incidents reported in the period)", ["Status", "Incidents", "Share"],
            [[s["label"], s["count"], _pct(s["pct"])] for s in summary["status"]])
    section("Incidents by category (all incidents reported in the period)", ["Category", "Incidents", "Share"],
            [[c["label"], c["count"], _pct(c["pct"])] for c in summary["categories"]])
    section(records_title(meta), RECORD_HEADINGS, [record_row(r) for r in records])
    if meta["truncated"]:
        w.writerow([f"*** END OF PARTIAL LIST - {meta['missing']:,} more matching incident(s) are not in this file ***"])
    return ("﻿" + out.getvalue()).encode("utf-8")  # BOM so Excel reads UTF-8


def build_pdf(summary, records, meta):
    try:
        from fpdf import FPDF
        from fpdf.fonts import FontFace
    except ImportError as exc:
        raise PdfUnavailable() from exc

    def text(value):
        # The built-in PDF fonts are Latin-1 only.
        return ("" if value is None else str(value)).replace("–", "-").replace("—", "-").encode("latin-1", "replace").decode("latin-1")

    pdf = FPDF(orientation="L", format="A4")
    pdf.set_auto_page_break(True, margin=12)
    pdf.set_margins(12, 12, 12)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 9, "ESRMS Analytics Report", new_x="LMARGIN", new_y="NEXT")
    if meta["truncated"]:
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_text_color(185, 28, 28)
        pdf.multi_cell(0, 5, text("WARNING: " + meta["status"]), border=1, padding=2, new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)
        pdf.ln(2)
    for name, value in header_rows(summary, meta):
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(78, 5, text(name))
        pdf.set_font("Helvetica", "", 9)
        pdf.multi_cell(0, 5, text(value), new_x="LMARGIN", new_y="NEXT")

    heading = FontFace(emphasis="BOLD", fill_color=(226, 232, 240))

    def title(label, note=None):
        pdf.ln(4)
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(0, 7, text(label), new_x="LMARGIN", new_y="NEXT")
        if note:
            pdf.set_font("Helvetica", "I", 8)
            pdf.cell(0, 4, text(note), new_x="LMARGIN", new_y="NEXT")

    def table(label, headings, rows, widths, note=None):
        title(label, note)
        pdf.set_font("Helvetica", "", 9)
        with pdf.table(col_widths=widths, headings_style=heading, line_height=5, text_align="LEFT") as t:
            t.row([text(v) for v in headings])
            for row in rows:
                t.row([text(v) for v in row])

    scope = "Calculated from " + SUMMARY_SCOPE + "."
    table("KPI summary", ["Metric", "Selected period", "Previous period", "Change"], kpi_rows(summary), (40, 20, 20, 30), scope)
    table("Incidents by status", ["Status", "Incidents", "Share"],
          [[s["label"], s["count"], _pct(s["pct"])] for s in summary["status"]], (50, 25, 25), scope)
    table("Incidents by category", ["Category", "Incidents", "Share"],
          [[c["label"], c["count"], _pct(c["pct"])] for c in summary["categories"]] or [["No incidents", "", ""]], (50, 25, 25), scope)

    title(records_title(meta), "Rows follow the record filters above. " + meta["status"])
    if records:
        _records_table(pdf, text, records)
        if meta["truncated"]:
            pdf.set_font("Helvetica", "B", 9)
            pdf.set_text_color(185, 28, 28)
            pdf.cell(0, 7, text(f"END OF PARTIAL LIST - {meta['missing']:,} more matching incident(s) are not in this file."),
                     new_x="LMARGIN", new_y="NEXT")
            pdf.set_text_color(0, 0, 0)
    return bytes(pdf.output())


def _records_table(pdf, text, records):
    """Fixed single-line rows drawn directly: about ten times faster than fpdf's table layout at the export limit."""
    columns = [("Incident ID", 22), ("Emergency Type", 78), ("Status", 28), ("Priority", 14), ("Location", 42),
               ("Response", 22), ("Resolution", 22), ("Reported (PHT)", 45)]
    height = 5.5

    def fit(value, width):
        value = text(value)
        if pdf.get_string_width(value) <= width - 2:
            return value
        while value and pdf.get_string_width(value + "...") > width - 2:
            value = value[:-1]
        return value + "..."

    def header():
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_fill_color(226, 232, 240)
        for name, width in columns:
            pdf.cell(width, height, name, border=1, fill=True)
        pdf.ln(height)
        pdf.set_font("Helvetica", "", 8)

    header()
    for r in records:
        if pdf.will_page_break(height):
            pdf.add_page()
            header()
        row = record_row(r)
        location = f"{row[5]:.5f}, {row[6]:.5f}" if r["has_location"] else "Unavailable"
        for value, (_, width) in zip([row[0], row[2], row[3], row[4], location, row[8], row[9], row[10]], columns):
            pdf.cell(width, height, fit(value, width), border=1)
        pdf.ln(height)


def filename(summary, extension, meta=None):
    p = summary["period"]
    partial = "_PARTIAL" if meta and meta["truncated"] else ""
    return f"esrms-analytics_{p['from']}_to_{p['to']}_{datetime.now(an.PHT):%Y%m%d-%H%M}{partial}.{extension}"
