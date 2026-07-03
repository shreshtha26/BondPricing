"""
Par-yield bootstrapping and curve visualization.

This module performs the project's central transformation: market par yields
are converted into discount factors and continuously compounded zero rates.
Those zero rates then feed ZeroCurve, forward-rate analytics, bond pricing, and
the interactive HTML chart.
"""

import json
import math
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Callable
from scipy.optimize import brentq
from int_rate_convention import validate_compounding_frequency, validate_rate, validate_time_years
from yield_curve import TOLERANCE, ZeroCurve, coupon_accrual_periods, coupon_payment_times, interpolate_curve_value


CurvePoint = dict[str, float | str]
CurveSeries = dict[str, str | list[CurvePoint]]


def _validated_par_curve_inputs(maturities: list[float], par_yields: list[float],
                                frequency: int) -> tuple[list[float], list[float]]:
    """
    Validates and sorts par-yield curve quotes before bootstrapping.
    Bootstrapping is sequential, so bad ordering, duplicate maturities, or
    missing short-end support can contaminate every later zero rate. This guard
    keeps the market quote set usable before any numerical solving begins.
    """
    if len(maturities) != len(par_yields):
        raise ValueError("maturities and par_yields must have the same length.")
    if not maturities:
        raise ValueError("At least one par-yield quote is required.")
    validate_compounding_frequency(frequency)
    points = sorted(zip(maturities, par_yields))
    sorted_maturities: list[float] = []
    sorted_par_yields: list[float] = []
    previous_maturity: float | None = None
    for maturity, par_yield in points:
        validate_time_years(maturity)
        validate_rate(par_yield, "par_yield")
        if maturity <= 0:
            raise ValueError("All maturities must be positive.")
        if previous_maturity is not None and math.isclose(maturity, previous_maturity, rel_tol=0.0, abs_tol=TOLERANCE):
            raise ValueError(f"Duplicate maturity: {maturity}.")
        sorted_maturities.append(float(maturity))
        sorted_par_yields.append(float(par_yield))
        previous_maturity = maturity
    first_coupon_time = 1 / frequency
    needs_coupon_dates = any(maturity > first_coupon_time + TOLERANCE for maturity in sorted_maturities)
    if needs_coupon_dates and sorted_maturities[0] > first_coupon_time + TOLERANCE:
        raise ValueError("The first maturity must be no later than the first coupon period "
            "so coupon cashflows can be discounted without extrapolation.")
    return sorted_maturities, sorted_par_yields


def _discount_factor_from_partial_curve(time: float, solved_maturities: list[float], solved_zero_rates: list[float],
                                        candidate_maturity: float, candidate_zero_rate: float) -> float:
    """
    Discounts a coupon cashflow while the bootstrap is still in progress.

    The current maturity's candidate zero rate is included with previously
    solved zero rates so the root solver can test whether the par bond prices
    exactly to 1.
    """
    if time == 0:
        return 1.0
    maturities = solved_maturities + [candidate_maturity]
    zero_rates = solved_zero_rates + [candidate_zero_rate]
    zero_rate = interpolate_curve_value(target_time=time, times=maturities, values=zero_rates, empty_error="At least one interpolation point is required.")
    return math.exp(-zero_rate * time)


def _solve_with_expanding_bracket(objective: Callable[[float], float], lower: float = -0.25,
                                  upper: float = 0.25, max_abs_bound: float = 5.0) -> float:
    """
    Solves a one-dimensional bootstrap equation with a widening rate bracket.

    Each curve node is found by forcing the corresponding par instrument to
    price at par. Expanding the bracket makes the solver more robust to unusual
    rate environments without hiding failure when no solution can be bracketed.
    """
    lower_value = objective(lower)
    upper_value = objective(upper)
    while lower_value * upper_value > 0:
        lower *= 2
        upper *= 2
        if abs(lower) > max_abs_bound or abs(upper) > max_abs_bound:
            raise ValueError("Could not bracket a zero-rate solution.")
        lower_value = objective(lower)
        upper_value = objective(upper)
    return brentq(objective, lower, upper)


def bootstrap_zero_rates_from_par_yields(maturities: list[float], par_yields: list[float],
                                         frequency: int = 2) -> list[float]:
    """
    Bootstraps continuously compounded zero rates from par yields.

    Each par yield is treated as the coupon rate of a par bond with face value
    1. Coupon dates that fall between solved curve nodes are discounted using
    linear interpolation on continuously compounded zero rates.

    Returned zero rates are sorted by increasing maturity.

    This is the core curve-construction routine. It turns observable market
    quotes into the zero rates needed by ZeroCurve for discounting, forwards,
    risk, and bond valuation.
    """
    sorted_maturities, sorted_par_yields = _validated_par_curve_inputs(maturities=maturities, par_yields=par_yields, frequency=frequency)
    solved_maturities: list[float] = []
    solved_zero_rates: list[float] = []
    for maturity, par_yield in zip(sorted_maturities, sorted_par_yields):
        payment_times = coupon_payment_times(maturity=maturity, frequency=frequency)
        accrual_periods = coupon_accrual_periods(maturity=maturity, frequency=frequency)
        def par_price_error(candidate_zero_rate: float) -> float:
            present_value = 0.0
            for payment_time, accrual_period in zip(payment_times, accrual_periods):
                cashflow = par_yield * accrual_period
                if math.isclose(payment_time, maturity, rel_tol=0.0, abs_tol=TOLERANCE):
                    cashflow += 1.0
                present_value += cashflow * _discount_factor_from_partial_curve(
                    time=payment_time,
                    solved_maturities=solved_maturities,
                    solved_zero_rates=solved_zero_rates,
                    candidate_maturity=maturity,
                    candidate_zero_rate=candidate_zero_rate,
                )
            return present_value - 1.0
        zero_rate = _solve_with_expanding_bracket(par_price_error)
        solved_maturities.append(maturity)
        solved_zero_rates.append(zero_rate)
    return solved_zero_rates


def bootstrap_discount_factors_from_par_yields(maturities: list[float], par_yields: list[float],
                                               frequency: int = 2) -> list[float]:
    """
    Bootstraps discount factors from par yields.

    Discount factors are the most direct pricing objects in fixed income. This
    helper exposes them alongside zero rates so reports and charts can show the
    full transformation from par yield to present-value curve.
    """
    sorted_maturities, _ = _validated_par_curve_inputs(maturities=maturities, par_yields=par_yields, frequency=frequency)
    zero_rates = bootstrap_zero_rates_from_par_yields(maturities=maturities, par_yields=par_yields, frequency=frequency)
    return [math.exp(-zero_rate * maturity) for maturity, zero_rate in zip(sorted_maturities, zero_rates)]


def curve_plot_series_from_par_yields(maturities: list[float], par_yields: list[float],
                                      frequency: int = 2) -> list[CurveSeries]:
    """
    Builds chart-ready series for par, spot/zero, discount, and forward curves.

    Rate series are expressed in percent. Discount factors are expressed as
    decimals and should be plotted on a separate axis.

    The generated series feed the interactive HTML chart and provide a visual
    audit trail for the bootstrapped curve: market inputs, derived zero rates,
    implied forwards, and discount factors are shown together.
    """
    sorted_maturities, sorted_par_yields = _validated_par_curve_inputs(maturities=maturities, par_yields=par_yields, frequency=frequency)
    zero_rates = bootstrap_zero_rates_from_par_yields(maturities=sorted_maturities, par_yields=sorted_par_yields, frequency=frequency)
    zero_curve = ZeroCurve(maturities=sorted_maturities, zero_rates=zero_rates)
    discount_factors = [zero_curve.discount_factor(maturity) for maturity in sorted_maturities]
    forward_points: list[CurvePoint] = []
    start_maturity = 0.0
    for end_maturity in sorted_maturities:
        forward_rate = zero_curve.forward_rate(start_maturity=start_maturity, end_maturity=end_maturity)
        forward_points.append(
            {
                "x": end_maturity,
                "y": forward_rate * 100,
                "label": (
                    f"{start_maturity:.3f}Y -> {end_maturity:.3f}Y: "
                    f"{forward_rate:.4%}"
                ),
            }
        )
        start_maturity = end_maturity
    return [
        {
            "name": "Par Yield",
            "axis": "rate",
            "color": "#2563eb",
            "points": [
                {
                    "x": maturity,
                    "y": par_yield * 100,
                    "label": f"{maturity:.3f}Y par yield: {par_yield:.4%}",
                }
                for maturity, par_yield in zip(sorted_maturities, sorted_par_yields)
            ],
        },
        {
            "name": "Spot / Zero Rate",
            "axis": "rate",
            "color": "#059669",
            "points": [
                {
                    "x": maturity,
                    "y": zero_rate * 100,
                    "label": f"{maturity:.3f}Y spot/zero: {zero_rate:.4%}",
                }
                for maturity, zero_rate in zip(sorted_maturities, zero_rates)
            ],
        },
        {
            "name": "Forward Rate",
            "axis": "rate",
            "color": "#dc2626",
            "points": forward_points,
        },
        {
            "name": "Discount Factor",
            "axis": "discount",
            "color": "#7c3aed",
            "points": [
                {
                    "x": maturity,
                    "y": discount_factor,
                    "label": f"{maturity:.3f}Y discount factor: {discount_factor:.6f}",
                }
                for maturity, discount_factor in zip(sorted_maturities, discount_factors)
            ],
        },
    ]


def _interactive_curve_html(series: list[CurveSeries], title: str) -> str:
    """
    Builds a self-contained interactive chart as an HTML string.

    Keeping the visualization dependency-free makes the project easy to run in
    a plain Python environment while still giving the user hover labels and
    series toggles for curve inspection.
    """
    chart_data = json.dumps(series)
    escaped_title = escape(title)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escaped_title}</title>
  <style>
    :root {{
      color-scheme: light;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #f8fafc;
      color: #0f172a;
    }}

    body {{
      margin: 0;
      padding: 24px;
    }}

    main {{
      max-width: 1180px;
      margin: 0 auto;
    }}

    h1 {{
      margin: 0 0 16px;
      font-size: 24px;
      font-weight: 650;
    }}

    .toolbar {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-bottom: 12px;
    }}

    label {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      min-height: 32px;
      padding: 0 10px;
      border: 1px solid #cbd5e1;
      border-radius: 6px;
      background: #ffffff;
      font-size: 13px;
      cursor: pointer;
      user-select: none;
    }}

    input {{
      margin: 0;
    }}

    .swatch {{
      width: 10px;
      height: 10px;
      border-radius: 50%;
      display: inline-block;
    }}

    .chart-wrap {{
      position: relative;
      overflow: hidden;
      border: 1px solid #cbd5e1;
      border-radius: 8px;
      background: #ffffff;
    }}

    svg {{
      display: block;
      width: 100%;
      height: min(68vh, 620px);
      min-height: 420px;
    }}

    .tooltip {{
      position: absolute;
      display: none;
      min-width: 160px;
      padding: 8px 10px;
      border: 1px solid #cbd5e1;
      border-radius: 6px;
      background: rgba(255, 255, 255, 0.96);
      box-shadow: 0 12px 28px rgba(15, 23, 42, 0.16);
      font-size: 12px;
      pointer-events: none;
      white-space: nowrap;
    }}

    .axis-label {{
      fill: #334155;
      font-size: 12px;
      font-weight: 600;
    }}

    .tick-label {{
      fill: #475569;
      font-size: 11px;
    }}

    .grid {{
      stroke: #e2e8f0;
      stroke-width: 1;
    }}

    .axis {{
      stroke: #94a3b8;
      stroke-width: 1;
    }}
  </style>
</head>
<body>
  <main>
    <h1>{escaped_title}</h1>
    <div id="toolbar" class="toolbar"></div>
    <div class="chart-wrap">
      <svg id="chart" viewBox="0 0 1100 620" role="img" aria-label="{escaped_title}"></svg>
      <div id="tooltip" class="tooltip"></div>
    </div>
  </main>
  <script>
    const seriesData = {chart_data};
    const visible = new Map(seriesData.map((series) => [series.name, true]));
    const svg = document.getElementById("chart");
    const toolbar = document.getElementById("toolbar");
    const tooltip = document.getElementById("tooltip");

    const bounds = {{
      left: 78,
      right: 76,
      top: 28,
      bottom: 62,
      width: 1100,
      height: 620
    }};

    const plot = {{
      left: bounds.left,
      right: bounds.width - bounds.right,
      top: bounds.top,
      bottom: bounds.height - bounds.bottom
    }};
    plot.width = plot.right - plot.left;
    plot.height = plot.bottom - plot.top;

    function makeSvg(tag, attrs = {{}}) {{
      const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
      for (const [key, value] of Object.entries(attrs)) {{
        node.setAttribute(key, value);
      }}
      return node;
    }}

    function allVisiblePoints(axis) {{
      return seriesData
        .filter((series) => visible.get(series.name) && (!axis || series.axis === axis))
        .flatMap((series) => series.points);
    }}

    function extent(values, fallbackMin, fallbackMax, padRatio = 0.08) {{
      if (values.length === 0) {{
        return [fallbackMin, fallbackMax];
      }}

      let minValue = Math.min(...values);
      let maxValue = Math.max(...values);

      if (minValue === maxValue) {{
        const pad = Math.max(Math.abs(minValue) * 0.05, 0.01);
        return [minValue - pad, maxValue + pad];
      }}

      const pad = (maxValue - minValue) * padRatio;
      return [minValue - pad, maxValue + pad];
    }}

    function xScale(value, xMin, xMax) {{
      return plot.left + ((value - xMin) / (xMax - xMin)) * plot.width;
    }}

    function yScale(value, yMin, yMax) {{
      return plot.bottom - ((value - yMin) / (yMax - yMin)) * plot.height;
    }}

    function addText(text, x, y, attrs = {{}}) {{
      const node = makeSvg("text", {{ x, y, ...attrs }});
      node.textContent = text;
      svg.appendChild(node);
      return node;
    }}

    function formatRate(value) {{
      return `${{value.toFixed(2)}}%`;
    }}

    function formatDiscount(value) {{
      return value.toFixed(3);
    }}

    function drawAxes(xMin, xMax, rateMin, rateMax, discountMin, discountMax) {{
      svg.appendChild(makeSvg("line", {{
        x1: plot.left, y1: plot.bottom, x2: plot.right, y2: plot.bottom, class: "axis"
      }}));
      svg.appendChild(makeSvg("line", {{
        x1: plot.left, y1: plot.top, x2: plot.left, y2: plot.bottom, class: "axis"
      }}));
      svg.appendChild(makeSvg("line", {{
        x1: plot.right, y1: plot.top, x2: plot.right, y2: plot.bottom, class: "axis"
      }}));

      for (let i = 0; i <= 6; i += 1) {{
        const ratio = i / 6;
        const xValue = xMin + ratio * (xMax - xMin);
        const x = xScale(xValue, xMin, xMax);
        svg.appendChild(makeSvg("line", {{
          x1: x, y1: plot.top, x2: x, y2: plot.bottom, class: "grid"
        }}));
        addText(`${{xValue.toFixed(2)}}Y`, x, plot.bottom + 24, {{
          "text-anchor": "middle",
          class: "tick-label"
        }});

        const rateValue = rateMin + ratio * (rateMax - rateMin);
        const rateY = yScale(rateValue, rateMin, rateMax);
        svg.appendChild(makeSvg("line", {{
          x1: plot.left, y1: rateY, x2: plot.right, y2: rateY, class: "grid"
        }}));
        addText(formatRate(rateValue), plot.left - 12, rateY + 4, {{
          "text-anchor": "end",
          class: "tick-label"
        }});

        const discountValue = discountMin + ratio * (discountMax - discountMin);
        addText(formatDiscount(discountValue), plot.right + 12, rateY + 4, {{
          "text-anchor": "start",
          class: "tick-label"
        }});
      }}

      addText("Maturity", (plot.left + plot.right) / 2, bounds.height - 16, {{
        "text-anchor": "middle",
        class: "axis-label"
      }});
      addText("Rate", 20, (plot.top + plot.bottom) / 2, {{
        "text-anchor": "middle",
        class: "axis-label",
        transform: `rotate(-90 20 ${{(plot.top + plot.bottom) / 2}})`
      }});
      addText("Discount Factor", bounds.width - 18, (plot.top + plot.bottom) / 2, {{
        "text-anchor": "middle",
        class: "axis-label",
        transform: `rotate(90 ${{bounds.width - 18}} ${{(plot.top + plot.bottom) / 2}})`
      }});
    }}

    function drawSeries(series, xMin, xMax, rateMin, rateMax, discountMin, discountMax) {{
      const yMin = series.axis === "discount" ? discountMin : rateMin;
      const yMax = series.axis === "discount" ? discountMax : rateMax;
      const points = series.points.map((point) => [
        xScale(point.x, xMin, xMax),
        yScale(point.y, yMin, yMax)
      ]);

      if (points.length > 1) {{
        svg.appendChild(makeSvg("polyline", {{
          points: points.map(([x, y]) => `${{x}},${{y}}`).join(" "),
          fill: "none",
          stroke: series.color,
          "stroke-width": 2.4,
          "stroke-linejoin": "round",
          "stroke-linecap": "round"
        }}));
      }}

      series.points.forEach((point, index) => {{
        const [x, y] = points[index];
        const circle = makeSvg("circle", {{
          cx: x,
          cy: y,
          r: 4,
          fill: "#ffffff",
          stroke: series.color,
          "stroke-width": 2,
          tabindex: 0
        }});

        circle.addEventListener("mousemove", (event) => {{
          tooltip.style.display = "block";
          tooltip.style.left = `${{event.offsetX + 14}}px`;
          tooltip.style.top = `${{event.offsetY + 14}}px`;
          tooltip.innerHTML = `<strong>${{series.name}}</strong><br>${{point.label}}`;
        }});
        circle.addEventListener("mouseleave", () => {{
          tooltip.style.display = "none";
        }});

        svg.appendChild(circle);
      }});
    }}

    function draw() {{
      svg.replaceChildren();

      const visiblePoints = allVisiblePoints();
      const ratePoints = allVisiblePoints("rate");
      const discountPoints = allVisiblePoints("discount");

      const [xMin, xMax] = extent(visiblePoints.map((point) => point.x), 0, 1, 0.04);
      const [rateMin, rateMax] = extent(ratePoints.map((point) => point.y), 0, 10, 0.12);
      const [discountMin, discountMax] = extent(
        discountPoints.map((point) => point.y),
        0.8,
        1.0,
        0.08
      );

      drawAxes(xMin, xMax, rateMin, rateMax, discountMin, discountMax);

      seriesData
        .filter((series) => visible.get(series.name))
        .forEach((series) => {{
          drawSeries(series, xMin, xMax, rateMin, rateMax, discountMin, discountMax);
        }});
    }}

    function buildToolbar() {{
      seriesData.forEach((series) => {{
        const label = document.createElement("label");
        const checkbox = document.createElement("input");
        const swatch = document.createElement("span");
        const text = document.createElement("span");

        checkbox.type = "checkbox";
        checkbox.checked = true;
        checkbox.addEventListener("change", () => {{
          visible.set(series.name, checkbox.checked);
          draw();
        }});

        swatch.className = "swatch";
        swatch.style.background = series.color;
        text.textContent = series.name;

        label.appendChild(checkbox);
        label.appendChild(swatch);
        label.appendChild(text);
        toolbar.appendChild(label);
      }});
    }}

    buildToolbar();
    draw();
  </script>
</body>
</html>
"""


def write_interactive_curve_html(maturities: list[float], par_yields: list[float], frequency: int = 2,
                                 output_path: str | Path = "sample_curve_plot.html",
                                 title: str = "Bootstrapped Curve Analytics") -> Path:
    """
    Writes an interactive HTML chart for par, spot, forward, and discount curves.

    This is the output boundary for visualization. Both sample curves and live
    FRED curves use this function, so the chart format stays consistent across
    demos and real market-data workflows.
    """
    series = curve_plot_series_from_par_yields(
        maturities=maturities,
        par_yields=par_yields,
        frequency=frequency,
    )
    output_file = Path(output_path)
    output_file.write_text(_interactive_curve_html(series=series, title=title), encoding="utf-8")
    return output_file


@dataclass
class BootstrappedZeroCurve:
    """
    Object-oriented wrapper around the par-yield bootstrap functions.

    The class keeps maturities, par yields, and coupon frequency together, which
    is convenient for examples, notebooks, and chart generation. The larger
    project uses the same bootstrap logic through market-data snapshots.
    """

    maturities: list[float]
    par_yields: list[float]
    frequency: int = 2

    def __post_init__(self) -> None:
        """
        Validates the quote set once when the curve builder is created.

        This makes later calls deterministic: discount factors, zero rates, and
        chart series all work from the same sorted and validated inputs.
        """
        self.maturities, self.par_yields = _validated_par_curve_inputs(maturities=self.maturities, par_yields=self.par_yields, frequency=self.frequency)

    def bootstrap_discount_factors(self) -> list[float]:
        """
        Bootstraps discount factors from par yields.

        This method is useful when you want the present-value curve directly,
        without first constructing a ZeroCurve object.
        """
        return bootstrap_discount_factors_from_par_yields(maturities=self.maturities, par_yields=self.par_yields, frequency=self.frequency)

    def bootstrap_zero_rates(self) -> list[float]:
        """
        Bootstraps continuously compounded zero rates from par yields.

        These zero rates are the spot curve used by the rest of the analytics
        stack for discounting and forward-rate calculations.
        """
        return bootstrap_zero_rates_from_par_yields(maturities=self.maturities, par_yields=self.par_yields, frequency=self.frequency)

    def curve_plot_series(self) -> list[CurveSeries]:
        """
        Returns chart-ready par, spot/zero, discount, and forward curve series.

        This is the object-oriented entry point for plotting a bootstrapped
        curve without manually calling the module-level chart helper.
        """
        return curve_plot_series_from_par_yields(maturities=self.maturities, par_yields=self.par_yields, frequency=self.frequency)

    def write_interactive_plot(self, output_path: str | Path = "sample_curve_plot.html",
                               title: str = "Bootstrapped Curve Analytics") -> Path:
        """
        Writes an interactive HTML chart for this bootstrapped curve.

        The sample workflow uses this method to produce sample_curve_plot.html;
        live FRED charts use the same renderer through market_data_loader.py.
        """
        return write_interactive_curve_html(
            maturities=self.maturities,
            par_yields=self.par_yields,
            frequency=self.frequency,
            output_path=output_path,
            title=title,
        )

    def to_zero_curve(self) -> ZeroCurve:
        """
        Converts the bootstrapped zero rates into a ZeroCurve object.

        This is the key handoff from curve construction to pricing. Once the
        ZeroCurve exists, all downstream valuation functions can consume it.
        """
        return ZeroCurve(maturities=self.maturities, zero_rates=self.bootstrap_zero_rates())


if __name__ == "__main__":
    curve = BootstrappedZeroCurve(
        maturities=[0.5, 1.0, 1.5, 2.0, 2.5, 3.0],
        par_yields=[0.038, 0.040, 0.041, 0.043, 0.044, 0.045],
        frequency=2,
    )
    discount_factors = curve.bootstrap_discount_factors()
    zero_rates = curve.bootstrap_zero_rates()
    print("Bootstrapping Example")
    print("-" * 40)
    for maturity, par_yield, df, zero_rate in zip(
        curve.maturities,
        curve.par_yields,
        discount_factors,
        zero_rates,
    ):
        print(
            f"Maturity: {maturity:>3}Y | "
            f"Par yield: {par_yield:.4%} | "
            f"DF: {df:.6f} | "
            f"Zero rate: {zero_rate:.4%}"
        )
    print()
    print("Using Bootstrapped Curve")
    print("-" * 40)
    zero_curve = curve.to_zero_curve()
    target_maturity = 2.25
    interpolated_rate = zero_curve.interpolate_rate(target_maturity)
    discount_factor = zero_curve.discount_factor(target_maturity)
    print(f"Target maturity: {target_maturity}Y")
    print(f"Interpolated zero rate: {interpolated_rate:.4%}")
    print(f"Discount factor: {discount_factor:.6f}")
    print()
    print("Forward Rate from Bootstrapped Curve")
    print("-" * 40)
    start_maturity = 1.0
    end_maturity = 3.0
    fwd = zero_curve.forward_rate(start_maturity=start_maturity, end_maturity=end_maturity)
    print(f"Start maturity: {start_maturity}Y")
    print(f"End maturity: {end_maturity}Y")
    print(f"Forward rate: {fwd:.4%}")
    print()
    print("Par Yield Check")
    print("-" * 40)
    maturity = 3.0
    par_yield = zero_curve.par_yield(maturity=maturity, frequency=2)
    print(f"Maturity: {maturity}Y")
    print(f"Par yield from bootstrapped curve: {par_yield:.4%}")
    print()
    print("Interactive Curve Plot")
    print("-" * 40)
    output_file = curve.write_interactive_plot(
        output_path="sample_curve_plot.html",
        title="Sample Bootstrapped Par, Spot, Forward, and Discount Curves",
    )
    print(f"Wrote interactive chart to: {output_file}")
