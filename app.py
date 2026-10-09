from pathlib import Path
import json

import numpy as np
import pandas as pd
import plotly.express as px
import streamlit as st

st.set_page_config(
    page_title="Automated Insight Generation",
    page_icon="📊",
    layout="wide",
)

INDICATORS = [
    "anc_coverage",
    "institutional_delivery",
    "immunization",
    "high_risk_cases",
]
PERCENTAGE_INDICATORS = [
    "anc_coverage",
    "institutional_delivery",
    "immunization",
]
REQUIRED_COLUMNS = ["month", "district", *INDICATORS]


def load_and_validate(uploaded_file):
    """Load a CSV and return cleaned data plus a validation report."""
    if uploaded_file is None:
        default_path = Path(__file__).parent / "healthcare_performance_expanded.csv"
        if not default_path.exists():
            return None, ["No CSV uploaded and no default dataset found."]
        raw = pd.read_csv(default_path)
        source_name = default_path.name
    else:
        raw = pd.read_csv(uploaded_file)
        source_name = uploaded_file.name

    missing_columns = sorted(set(REQUIRED_COLUMNS) - set(raw.columns))
    if missing_columns:
        raise ValueError(f"Missing required columns: {', '.join(missing_columns)}")

    report = [
        f"Source: {source_name}",
        f"Input rows: {len(raw)}",
        f"Exact duplicate rows: {int(raw.duplicated().sum())}",
    ]

    data = raw.copy()
    data["month"] = pd.to_datetime(data["month"], errors="coerce")
    data["district"] = data["district"].astype("string").str.strip()
    for column in INDICATORS:
        data[column] = pd.to_numeric(data[column], errors="coerce")

    invalid_mask = (
        data["month"].isna()
        | data["district"].isna()
        | data["district"].eq("")
        | data[INDICATORS].isna().any(axis=1)
    )
    duplicate_key_mask = data.duplicated(
        subset=["district", "month"], keep="first"
    )

    report.append(f"Rows with invalid required fields: {int(invalid_mask.sum())}")
    report.append(f"Duplicate district-month records: {int(duplicate_key_mask.sum())}")

    cleaned = data.loc[~invalid_mask & ~duplicate_key_mask].copy()
    cleaned = cleaned.sort_values(["district", "month"]).reset_index(drop=True)
    report.append(f"Rows retained for analysis: {len(cleaned)}")

    range_issues = {}
    for column in PERCENTAGE_INDICATORS:
        count = int((~cleaned[column].between(0, 100)).sum())
        if count:
            range_issues[column] = count
    if range_issues:
        report.append(f"Percentage values outside 0 to 100 (review): {range_issues}")
    else:
        report.append("Percentage range check: no values outside 0 to 100")

    return cleaned, report


def detect_trends(data, indicators, threshold_pct):
    """Compare each district's current observation with its previous observation."""
    records = []
    ordered = data.sort_values(["district", "month"])

    for indicator in indicators:
        previous = ordered.groupby("district")[indicator].shift(1)
        current = ordered[indicator]
        valid_previous = previous.notna() & previous.ne(0)
        change_pct = (
            (current - previous) / previous.where(valid_previous) * 100
        )

        mask = change_pct.notna() & (change_pct.abs() >= threshold_pct)
        for idx in ordered.index[mask]:
            row = ordered.loc[idx]
            records.append({
                "type": "trend",
                "indicator": indicator,
                "entity": str(row["district"]),
                "period": row["month"].strftime("%Y-%m"),
                "value": float(row[indicator]),
                "prev_value": float(previous.loc[idx]),
                "change_pct": float(change_pct.loc[idx]),
                "metric": "percentage_change",
            })
    return pd.DataFrame(records)


def detect_outliers(data, indicators, multiplier):
    """Flag values outside global IQR bounds for each indicator."""
    records = []
    for indicator in indicators:
        q1 = data[indicator].quantile(0.25)
        q3 = data[indicator].quantile(0.75)
        iqr = q3 - q1
        lower = q1 - multiplier * iqr
        upper = q3 + multiplier * iqr

        flagged = data.loc[
            (data[indicator] < lower) | (data[indicator] > upper)
        ]
        for _, row in flagged.iterrows():
            records.append({
                "type": "outlier",
                "indicator": indicator,
                "entity": str(row["district"]),
                "period": row["month"].strftime("%Y-%m"),
                "value": float(row[indicator]),
                "prev_value": None,
                "change_pct": None,
                "metric": "iqr_outlier",
                "lower_bound": float(lower),
                "upper_bound": float(upper),
            })
    return pd.DataFrame(records)


def detect_correlations(data, indicators, threshold):
    """Return Pearson matrix and unique pairs whose absolute r meets threshold."""
    matrix = data[indicators].corr(method="pearson")
    records = []
    for i, first in enumerate(indicators):
        for second in indicators[i + 1:]:
            value = matrix.loc[first, second]
            if pd.notna(value) and abs(value) >= threshold:
                records.append({
                    "type": "correlation",
                    "indicator": first,
                    "related_indicator": second,
                    "entity": "All districts",
                    "period": "Full dataset",
                    "value": float(value),
                    "metric": "pearson_r",
                })
    return matrix, pd.DataFrame(records)


def trend_severity(change_pct, threshold_pct):
    """Example review priority, not a clinical risk classification."""
    if abs(change_pct) >= 1.5 * threshold_pct:
        return "High"
    return "Medium"


def generate_insights(trends, outliers, correlations, threshold_pct):
    """Build structured insight records from computed results."""
    records = []

    for row in trends.to_dict("records"):
        change = row["change_pct"]
        direction = "increased" if change > 0 else "decreased"
        records.append({
            "type": "trend",
            "indicator": row["indicator"],
            "entity": row["entity"],
            "period": row["period"],
            "metric": row["value"],
            "change": change,
            "value": row["value"],
            "prev_value": row["prev_value"],
            "change_pct": change,
            "severity": trend_severity(change, threshold_pct),
            "explanation": (
                f"{row['indicator']} in {row['entity']} {direction} by "
                f"{abs(change):.1f}% in {row['period']} compared with the "
                f"previous available month. This meets the configured "
                f"{threshold_pct:.1f}% threshold."
            ),
        })

    for row in outliers.to_dict("records"):
        records.append({
            "type": "outlier",
            "indicator": row["indicator"],
            "entity": row["entity"],
            "period": row["period"],
            "metric": row["value"],
            "change": None,
            "value": row["value"],
            "prev_value": None,
            "change_pct": None,
            "severity": "Medium",
            "explanation": (
                f"{row['indicator']} in {row['entity']} during {row['period']} "
                "falls outside the dataset's IQR bounds and should be reviewed."
            ),
        })

    for row in correlations.to_dict("records"):
        value = row["value"]
        records.append({
            "type": "correlation",
            "indicator": f"{row['indicator']}:{row['related_indicator']}",
            "entity": row["entity"],
            "period": row["period"],
            "metric": value,
            "change": None,
            "value": value,
            "prev_value": None,
            "change_pct": None,
            "severity": "Medium",
            "explanation": (
                f"{row['indicator']} and {row['related_indicator']} have a "
                f"Pearson correlation of {value:.2f}. Correlation does not "
                "establish causation."
            ),
        })

    columns = [
        "insight_id", "type", "indicator", "entity", "period", "metric",
        "change", "value", "prev_value", "change_pct", "severity", "explanation",
    ]
    if not records:
        return pd.DataFrame(columns=columns)

    result = pd.DataFrame(records)
    result.insert(
        0, "insight_id",
        [f"INS-{i:04d}" for i in range(1, len(result) + 1)],
    )
    return result[columns]


st.title("Automated Insight Generation")
st.caption(
    "Explore district-level healthcare indicators, detect statistical patterns, "
    "and review automatically generated insights."
)

with st.sidebar:
    st.header("Data and thresholds")
    uploaded_file = st.file_uploader("Upload healthcare CSV", type=["csv"])
    trend_threshold = st.slider(
        "Trend change threshold (%)", min_value=1, max_value=50,
        value=10, step=1,
    )
    iqr_multiplier = st.slider(
        "IQR outlier multiplier", min_value=0.5, max_value=3.0,
        value=1.5, step=0.5,
    )
    correlation_threshold = st.slider(
        "Absolute correlation threshold", min_value=0.1, max_value=1.0,
        value=0.7, step=0.05,
    )

try:
    data, validation_report = load_and_validate(uploaded_file)
except Exception as exc:
    st.error(f"Could not process this CSV: {exc}")
    st.stop()

if data is None or data.empty:
    st.warning("No valid records are available for analysis.")
    st.stop()

with st.expander("Data validation report", expanded=False):
    for line in validation_report:
        st.write(f"• {line}")

with st.sidebar:
    districts = sorted(data["district"].dropna().unique().tolist())
    selected_districts = st.multiselect(
        "Districts", options=districts, default=districts
    )
    available_months = sorted(data["month"].dt.strftime("%Y-%m").unique().tolist())
    selected_months = st.multiselect(
        "Months", options=available_months, default=available_months
    )
    selected_indicators = st.multiselect(
        "Indicators", options=INDICATORS, default=INDICATORS
    )

if not selected_districts or not selected_months or not selected_indicators:
    st.info("Select at least one district, month, and indicator.")
    st.stop()

filtered = data.loc[
    data["district"].isin(selected_districts)
    & data["month"].dt.strftime("%Y-%m").isin(selected_months)
].copy()

if filtered.empty:
    st.warning("No rows match the selected filters.")
    st.stop()

trends = detect_trends(filtered, selected_indicators, trend_threshold)
outliers = detect_outliers(filtered, selected_indicators, iqr_multiplier)
correlation_matrix, correlations = detect_correlations(
    filtered, selected_indicators, correlation_threshold
)
insights = generate_insights(trends, outliers, correlations, trend_threshold)

high_count = int((insights["severity"] == "High").sum()) if not insights.empty else 0
col1, col2, col3, col4 = st.columns(4)
col1.metric("Records analyzed", f"{len(filtered):,}")
col2.metric("Trends flagged", f"{len(trends):,}")
col3.metric("Potential outliers", f"{len(outliers):,}")
col4.metric("Insights generated", f"{len(insights):,}")

st.subheader("Generated insights")
if insights.empty:
    st.info("No findings meet the selected thresholds. Try adjusting the filters or thresholds.")
else:
    severity_filter = st.multiselect(
        "Filter by severity",
        options=["High", "Medium", "Low"],
        default=["High", "Medium", "Low"],
    )
    visible_insights = insights.loc[insights["severity"].isin(severity_filter)]
    st.dataframe(visible_insights, use_container_width=True, hide_index=True)

    json_bytes = visible_insights.to_json(
        orient="records", indent=2, force_ascii=False
    ).encode("utf-8")
    st.download_button(
        "Download insights JSON",
        data=json_bytes,
        file_name="insights.json",
        mime="application/json",
    )
    st.download_button(
        "Download insights CSV",
        data=visible_insights.to_csv(index=False).encode("utf-8"),
        file_name="insights.csv",
        mime="text/csv",
    )

    severity_counts = (
        visible_insights["severity"]
        .value_counts()
        .reindex(["Low", "Medium", "High"], fill_value=0)
        .rename_axis("severity")
        .reset_index(name="count")
    )
    fig = px.bar(
        severity_counts, x="severity", y="count",
        title="Insights by Severity", text="count",
        category_orders={"severity": ["Low", "Medium", "High"]},
    )
    st.plotly_chart(fig, use_container_width=True)

st.subheader("Correlation analysis")
st.caption(
    "Pearson correlation is exploratory and does not imply causation. "
    "Small datasets can produce unstable correlations."
)
fig_corr = px.imshow(
    correlation_matrix,
    text_auto=".2f",
    zmin=-1,
    zmax=1,
    color_continuous_scale="RdBu_r",
    aspect="auto",
    title="Pearson Correlation Heatmap",
)
st.plotly_chart(fig_corr, use_container_width=True)

st.subheader("Indicator trends by district")
indicator_to_plot = st.selectbox(
    "Indicator to visualize", options=selected_indicators
)
line_fig = px.line(
    filtered.sort_values("month"),
    x="month",
    y=indicator_to_plot,
    color="district",
    markers=True,
    title=f"{indicator_to_plot} over time",
)
st.plotly_chart(line_fig, use_container_width=True)

st.subheader("Download analysis outputs")
st.download_button(
    "Download correlation matrix CSV",
    data=correlation_matrix.to_csv().encode("utf-8"),
    file_name="correlation_matrix.csv",
    mime="text/csv",
)
