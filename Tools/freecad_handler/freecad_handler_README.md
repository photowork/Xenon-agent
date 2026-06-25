# FreeCAD Handler

`Tools/freecad_handler/freecad_handler.py` lets Xenon create and inspect FreeCAD models through
structured JSON-friendly actions. Modeling runs in FreeCAD's bundled Python
interpreter, isolated from the Xenon process.

## Load

Load module `freecad_handler`, then call:

- `freecad_handler_FreeCAD_describe_capabilities`
- `freecad_handler_FreeCAD_status`
- `freecad_handler_FreeCAD_execute_scenario`
- `freecad_handler_FreeCAD_inspect_document`
- `freecad_handler_FreeCAD_export_document`
- `freecad_handler_FreeCAD_render_preview`
- `freecad_handler_FreeCAD_open_in_gui`
- `freecad_handler_FreeCAD_install_collaboration_addon`
- `freecad_handler_FreeCAD_prepare_current_session_bridge`
- `freecad_handler_FreeCAD_start_live_session`
- `freecad_handler_FreeCAD_inspect_live_context`
- `freecad_handler_FreeCAD_execute_live_step`
- `freecad_handler_FreeCAD_live_undo`
- `freecad_handler_FreeCAD_live_redo`
- `freecad_handler_FreeCAD_execute_live_scenario`
- `freecad_handler_FreeCAD_render_live_preview`
- `freecad_handler_FreeCAD_inspect_object_properties`
- `freecad_handler_FreeCAD_inspect_live_object_properties`
- `freecad_handler_FreeCAD_inspect_object_topology`
- `freecad_handler_FreeCAD_inspect_live_object_topology`
- `freecad_handler_FreeCAD_describe_action`
- `freecad_handler_FreeCAD_validate_scenario`
- `freecad_handler_FreeCAD_analyze_drawing_image`
- `freecad_handler_FreeCAD_create_model_from_drawing`
- `freecad_handler_FreeCAD_compare_visuals`
- `freecad_handler_FreeCAD_evaluate_visual_iteration`

Lengths use millimeters. Angles use degrees.

## Example

```json
{
  "document_name": "mounting_plate",
  "actions": [
    {"op": "box", "id": "plate", "length": 100, "width": 60, "height": 5},
    {"op": "cylinder", "id": "h1", "radius": 4, "height": 5, "position": [10, 10, 0]},
    {"op": "cylinder", "id": "h2", "radius": 4, "height": 5, "position": [90, 10, 0]},
    {"op": "cylinder", "id": "h3", "radius": 4, "height": 5, "position": [90, 50, 0]},
    {"op": "cylinder", "id": "h4", "radius": 4, "height": 5, "position": [10, 50, 0]},
    {"op": "cut", "id": "plate_cut_1", "base": "plate", "tool": "h1"},
    {"op": "cut", "id": "plate_cut_2", "base": "plate_cut_1", "tool": "h2"},
    {"op": "cut", "id": "plate_cut_3", "base": "plate_cut_2", "tool": "h3"},
    {"op": "cut", "id": "finished_plate", "base": "plate_cut_3", "tool": "h4"}
  ],
  "save_path": "output/freecad/mounting_plate.FCStd",
  "exports": ["step", "stl"],
  "preview": {"orientation": "axonometric"},
  "visual_mode": true,
  "step_delay": 1.0,
  "overwrite": true
}
```

## Watch The Drawing Live

Set `visual_mode` to `true` to launch an independent FreeCAD window before
modeling starts. The window refreshes after every action:

```json
{
  "visual_mode": true,
  "step_delay": 1.5,
  "fit_after_each_step": true,
  "keep_gui_open": true,
  "final_hold_seconds": 3
}
```

- `step_delay`: pause after every action, from 0 to 30 seconds.
- `fit_after_each_step`: fit and show the model axonometrically after each step.
- `keep_gui_open`: after drawing and saving, hand the completed model to a
  detached FreeCAD viewer. The Xenon tool call returns immediately while the
  viewer remains open.
- `final_hold_seconds`: when `keep_gui_open` is false, keep the completed model
  visible for this many seconds before closing.
- An individual action may set `visual_delay` to override `step_delay`.

This mode opens an isolated FreeCAD instance controlled by the worker. It does
not attach to a FreeCAD instance that was already opened manually. When
`keep_gui_open` is enabled, the returned `visual_session.process_id` identifies
the detached final viewer. Continue editing by using the returned `save_path`
as the next scenario's `source_path`.

## Persistent And Current GUI Sessions

Use `start_live_session` when Xenon should open one visible FreeCAD window and
continue modifying the same active document over many tool calls. The call
returns after the bridge is ready; the GUI remains open.

To control a FreeCAD session that the user opened manually:

1. Recommended one-time setup: call `install_collaboration_addon`, then restart
   FreeCAD once.
2. Call `prepare_current_session_bridge`. With the addon installed, the open
   FreeCAD session attaches automatically.
3. Without the addon, run the returned `XenonLiveBridge.FCMacro` once from
   FreeCAD's Macro dialog.
4. Call `live_status`, then use `inspect_live_context` and `execute_live_step`.

`stop_live_bridge` disconnects Xenon but deliberately leaves the user's GUI
open. The bridge only listens on `127.0.0.1` and requires a random token.

### Human-Agent Collaboration

When the user is drawing manually and only needs help with one step:

1. Select the relevant object, edges, or faces in the current FreeCAD window.
2. Call `inspect_live_context` so the agent can see the active document, active
   Body, selection order, and selected subelements.
3. Call `execute_live_step` with one action.
4. Continue drawing manually. The FreeCAD window remains open, the camera is
   preserved, and the document is not automatically saved.

`execute_live_step` creates one FreeCAD transaction, so `live_undo` and
`live_redo` can immediately reject or restore the assisted step.

Selection-aware examples:

```json
{"op": "fillet", "radius": 2}
```

The selected object becomes `source` and selected edges become `edges`.

```json
{"op": "set_properties", "properties": {"Length": 35}}
```

The selected feature becomes `object`.

```json
{"op": "cut"}
```

The first selected object becomes `base` and the second becomes `tool`.

Explicit placeholders are also accepted: `$selection`, `$selection1`,
`$selection2`, `$selections`, `$selected_edges`, `$selected_faces`,
`$selected_subelements`, `$active_body`, and `$tip`.

Use `set_properties` to edit and recompute existing objects:

```json
{"op": "set_properties", "object": "Pad", "properties": {"Length": 35, "Reversed": false}}
```

Inspect available values first with `inspect_object_properties` or
`inspect_live_object_properties`.

## Supported Actions

Before using an unfamiliar operation, call `describe_action` for its exact
required and optional parameters. High-risk operations are validated before
FreeCAD starts. Call `validate_scenario` to preflight a complete action list
without launching FreeCAD.

Important parameter conventions:

| Operation | Correct parameter convention |
| --- | --- |
| `rotate`, `move`, `copy` | Use `source`, never `target` |
| `fillet`, `chamfer` | Use `source`; `edges` is a non-empty array of unique 1-based integers |
| `box position` | Starting corner |
| `cylinder position` | Center of the cylinder base |
| `torus center` | Center of the torus; `position` is accepted as an alias |

Use `inspect_object_topology` or `inspect_live_object_topology` before fillet
or chamfer. These methods return `Edge1`, `Edge2`, etc. with lengths and
positions, so the agent does not need to guess.

### Primitive solids

- `box`: `length`, `width`, `height`, optional `position`, `direction`
- `cylinder`: `radius`, `height`, optional `position`, `direction`, `angle`
- `sphere`: `radius`, optional `center`, `axis`, `angle1`, `angle2`, `angle3`
- `cone`: `radius1`, `radius2`, `height`
- `torus`: `radius1`, `radius2`, optional `center` or alias `position`

### 2D and profiles

- `line`: `start`, `end`
- `circle`: `center`, `normal`, `radius`
- `arc`: `start`, `mid`, `end`
- `polyline`: `points`, optional `closed`, `face`
- `rectangle`: `x`, `y`, `z`, `width`, `height`, optional `face`
- `bspline`: `poles` (2+ control points), optional `degree`, `closed`, `knots`, `weights`, `face`
- `bezier`: `poles` (2+ control points), optional `degree`, `face`
- `create_sketch`: `geometry`, optional solver-driven `constraints`

Sketch geometry supports `line`, `circle`, `arc`, `bspline`, and `bezier`. Constraints use a FreeCAD
constraint type plus an argument list:

```json
{
  "op": "create_sketch",
  "id": "profile",
  "geometry": [
    {"type": "line", "start": [0, 0, 0], "end": [50, 0, 0]},
    {"type": "line", "start": [50, 0, 0], "end": [50, 30, 0]}
  ],
  "constraints": [
    {"type": "horizontal", "args": [0]},
    {"type": "vertical", "args": [1]},
    {"type": "coincident", "args": [0, 2, 1, 1]}
  ]
}
```

### Modeling

- `extrude`: `source`, then `vector` or `length`
- `revolve`: `source`, optional `base`, `axis`, `angle`
- `fuse`, `cut`, `common`: `base`, then `tool` or `tools`
- `copy`, `move`, `rotate`: always reference the input with `source`
- `fillet`: `source`, `radius`, required one-based `edges`; use `all_edges=true` deliberately
- `chamfer`: `source`, `size`, required one-based `edges`; use `all_edges=true` deliberately
- `remove`: `object`

### Part Design

- `create_body`
- `create_datum_plane`: `attachment` (xy/xz/yz or object ref), optional `body`, `offset`, `placement`
- `create_datum_axis`: `attachment` (x/y/z or object ref), optional `body`, `placement`
- `create_datum_point`: optional `body`, `attachment`, `position` (3-vector), `placement`
- `create_sketch`: add `body` and optional `support` (`xy`, `xz`, `yz`)
- `pad`, `pocket`: `body`, `profile`, `length`
- `partdesign_linear_pattern`: `body`, `original`, `direction`, `length`, `occurrences`
- `partdesign_polar_pattern`: `body`, `original`, `axis`, `angle`, `occurrences`
- `partdesign_mirror`: `body`, `original`, `plane`
- `partdesign_thickness`: `body`, `source`, `faces`, `thickness`
- `groove`: `body`, `profile`, optional `axis` (`x`/`y`/`z` or 3-vector), `angle`, `reversed`, `midplane`
- `hole`: `body`, `profile`, optional `hole_type` (through/blind/counterbore/countersink), `thread_type` (none/metric/metric_fine), `diameter`, `depth`, `thread_depth`, `drill_point` (flat/angled/sphere), `model_thread`
- `partdesign_pipe`: `body`, `path`, optional `profile`/`profiles`, `transition` (transformed/linear/frenet), `keep_source`

### Advanced Features

- `linear_array`, `polar_array`, `mirror`, `shell`
- `loft`: `profiles` (2+ object names), optional `ruled`, `closed`, `keep_profiles`
- `sweep`: `path` (object name), `profiles` (list), optional `make_solid`, `frenet`, `keep_source`
- `thread_helix`: a cosmetic/centerline helix
- `thread`: a swept triangular thread solid; optionally fuse or cut it with `source`
- `create_assembly`, `assembly_link`

#### Groove, loft, and sweep examples

Groove cuts a slot by revolving a sketch around an axis:

```json
{"op": "groove", "id": "slot", "body": "body", "profile": "slot_sketch", "axis": "z", "angle": 360}
```

Loft transitions through multiple profile sketches:

```json
{"op": "loft", "id": "transition", "profiles": ["circle_bottom", "square_mid", "circle_top"], "ruled": true}
```

Sweep extrudes a profile along a path wire:

```json
{"op": "sweep", "id": "tube", "path": "spine_wire", "profiles": ["ring_section"], "make_solid": true}
```

### TechDraw

- `techdraw_page`: creates an SVG-template page
- `techdraw_view`: places a source object on a page
- `techdraw_dimension`: adds a projected dimension with optional upper/lower tolerance
- `techdraw_section_view`: creates a section view with `section_direction` (x/y/z or 3-vector), `section_origin`, `scale`
- `techdraw_annotation`: adds a text annotation with `text`, `x`, `y`, `font_size`
- `techdraw_balloon`: adds a callout balloon with `text`, `view`, `origin_x`/`origin_y`, `x`/`y`

### Draft Workbench

- `shape_string`: `string`, `font_path` (.ttf/.otf), optional `size`, `extrude`, `position`
- `draft_array`: `source`, `array_type` (orthogonal/polar), for orthogonal use `count_x`/`count_y`/`interval_x`/`interval_y`
- `draft_clone`: `source`, optional `placement`
- `import_dxf`: `file_path`, optional `as_sketch`, `layer_filter`

## Drawing Recognition And Visual Iteration

`analyze_drawing_image` is layout/visual first. When a PaddleOCR-VL sidecar JSON
exists next to the source image, it is used to find drawing view regions and
metadata such as material, unit, scale, and title-block text. OpenCV line/circle
detection then runs inside those view regions; without a sidecar it falls back
to the full image.

OCR is auxiliary metadata only. Tesseract text, OCR dimensions, thread specs,
angles, and surface roughness are returned as candidates for human/agent review,
but they are not modeling authority.

`create_model_from_drawing` no longer auto-reconstructs a model by default from
OCR dimensions. The preferred flow is `analyze_drawing_image`, review the visual
evidence, build explicit structured actions, then use `execute_scenario`,
`render_preview`, and `inspect_document` to validate the FreeCAD result. The old
simple top-view plate experiment remains available only with
`allow_legacy_ocr_plate=true` and always reports `review_required=true`.

`compare_visuals` scores a rendered image against a reference. Use
`evaluate_visual_iteration`, or `render_live_preview` plus `compare_visuals`,
to repeat the render/compare/edit loop.

Every shape-producing action requires a unique `id`. Later actions reference
earlier shapes by this ID.

Boolean operations, extrude, revolve, move, rotate, fillet, chamfer, groove,
loft, and sweep hide their source objects by default. Use `keep_source`,
`keep_sources`, or `keep_profiles` (for `loft`) when the source objects should
remain visible.

## Exports and Preview

Supported exports: STEP, IGES, STL, OBJ, AMF, BREP, SVG, DXF.

An export item can be a format string or:

```json
{
  "format": "step",
  "file_path": "output/freecad/result.step",
  "object_ids": ["finished_plate"]
}
```

Preview rendering uses FreeCAD GUI in the isolated worker. The tool returns
`preview_error` if rendering is unavailable. Set `preview.required` to `true`
when preview failure must fail the whole task.

## Safety

- Default file access is restricted to the Xenon workspace.
- Set `allow_external_paths=true` only for an explicitly requested external path.
- Existing files are not replaced unless `overwrite=true`.
- Scenarios are limited to 200 actions and 500 document objects.
- Fillet/chamfer are limited to 64 selected edges and reject obviously excessive
  radii/sizes before invoking the geometry kernel. An explicit unsafe override
  exists for expert use.
- Each action runs in a FreeCAD document transaction. A normal Python-level
  action failure is rolled back before later actions continue.
- Each run writes request, response, and process log files under
  `output/freecad/.jobs/`.
