# E-SFR 2023 chart source and implementation boundaries

Reviewed on 2026-10-18. Target: the user's Imatest Extended E-SFR ISO 2023 chart.

## Source hierarchy

1. [Current Imatest chart instructions](https://docs.imatest.com/docs/esfriso_instructions) identify the Extended chart's added stars and squares, patch counts, and nominal D50 CIELAB and gray-density design targets.
2. The [official linked PNG](https://docs.imatest.com/assets/wp-content/uploads/2018/09/23.2-12233-chart-web.png) is 640 by 370 pixels. Its decoded RGB pixels exactly match the user's attachment. It establishes which chart variant is being reconstructed. It is a preview, not a high-resolution optical input.
3. [Kerr's EI2024 primary paper](https://www.imatest.com/wp-content/uploads/2024/03/Kerr_EI2024_IQSP_Recommendations_for_the_Detection_and_Analysis_of_the_ISO_12233-2023_e-SFR_Slanted_Star.pdf) confirms nine core stars plus four corner stars and eight interior edge regions per star. It discusses why acute star angles make consistent rectangular ROI placement difficult.
4. [Imatest's logarithmic-wedge explanation](https://www.imatest.com/2021/12/logarithmic-wedges/) defines exponentially varying frequency along the wedge. Frequency labels require reference framing; chart pixels alone are insufficient for an absolute frequency calibration.
5. [ISO's 2023 catalogue entry](https://www.iso.org/standard/79169.html) confirms that edition 4 was replaced by [ISO 12233:2024](https://www.iso.org/standard/88626.html). Keep chart revision and analysis algorithm revision as separate fields.

Downloaded originals, hashes, nominal numerical tables, and unresolved differences are in `references/sources.json` at the release root, or `../../references/sources.json` relative to this document's directory. No publicly linked vendor SVG for this exact 2023 Extended variant was found. The Imatest SVG documentation explains generating charts with its Test Charts software; its direct public SVG examples are older generic charts.

## Geometry that can be implemented now

The target has 13 four-cycle slanted stars, two side squares, four registration circles, two orientation dots, 20 gray patches, and 18 color patches. The eight radial edges of each star can be indexed separately from the four outer square edges. The side squares provide eight more edges. Feature locations can be reconstructed in normalized chart coordinates, then mapped to native camera coordinates by registration. Measurement must use native samples; rectifying the image to fit the drawing would change measured MTF.

The preview's whole raster is 640 by 370, not 16:9. Preserve the artwork's aspect and record the active framing rectangle independently. The product table also describes outside-border dimensions separately from the nominal 16:9 name. Avoid anisotropic stretching.

## Differences that must remain visible

- Imatest's product prose says six extra stars; the detailed chart instructions, user artwork, and Kerr Figure 2 show four extra stars plus two side squares. The implementation follows the supplied variant.
- Imatest describes 12 wedge pairs, but the artwork contains 16 separately visible wedge components: six outer vertical, four around the center, and six medium components. The public sources inspected do not establish how this description maps to the components. Preserve the visible shapes and record the unresolved pairing; do not fabricate extra patterns to force a count of 24.
- The PDF info sheet linked by the product page is dated 2021 and describes the 2017 chart. It is not evidence for 2023 star geometry.
- Several paragraphs of current web documentation still describe 2017 chart variants. Reference the specific 2023 sections and the supplied image rather than merging every count on the page.

## Radiometry and sampling

Use analytic polygons and logarithmic curves with controlled subpixel integration. Do not enlarge the 640-pixel screenshot and treat interpolation blur as the camera's optics. Keep rendered sample pitch, antialias integration, optical blur, sensor pixel integration, CFA sampling, and ISP transfer explicit. Check convergence before using the rendered chart to fit PSF.

The 4:1 design contrast is a ratio of linear reflectance, not a ratio of display RGB codes. Official nominal gray densities and D50 Lab colors are useful synthetic design targets. They are not measurements of the user's printed chart; the manufacturer explicitly says actual values vary, with substrate-dependent dark-density limits. Lab values also do not determine a unique spectral reflectance curve. Preserve chart reflectance, spectral data, physical dimensions, substrate, and chart MTF as unmeasured until supplied or measured.

## Analysis scope

Kerr's paper reports the 2023 edge-angle correction, fifth-order polynomial fitting, and Hann window. Current Imatest describes Tukey alpha one and fifth-order fitting for its 2024-compatible options; Hann is the alpha-one Tukey case. These references can guide an implementation, but the exact full ISO Annex C and D text has not been inspected: the publicly accessible OBP response was only a JavaScript application shell. No claim of ISO conformance follows from recreating the chart image.

The paper recommends rectangular ROIs of 100 to 400 pixels per side, with documented exceptions, and requires an edge crossing opposite sides. Small star-arm crops may need exceptions; report native ROI dimensions, edge orientation, usable edge length, and rejection reasons. Diagonal edge accuracy must be tested independently from near-horizontal/vertical edges. A chart-specific model should expose all eight star arms and retain a clear exclusion reason where a reliable ROI cannot fit.
