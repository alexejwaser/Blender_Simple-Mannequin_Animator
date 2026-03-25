# Simple Mannequin Animator

![Simple Mannequin Animator](title_animation.gif)

A Blender add-on for spring-physics character animation using simple head + body objects.

Animate an **Empty controller** — the head follows automatically, and the body leans back when accelerating, swinging forward when braking, driven by a real spring simulation.

---

## Features

- **Empty controller** — animate one object to drive position and Z rotation of the whole character
- **Spring physics** — semi-implicit Euler simulation with tunable stiffness and damping
- **Inertia tilt** — body leans opposite to travel direction, independent of head Z rotation
- **Curve & sideways movement** — tilt axis is derived from world-space velocity, so the character works correctly on curves and moving in any direction
- **Overshoot & oscillation** — body bounces naturally when stopping suddenly
- **Multi-mannequin** — manage multiple independent characters in one scene, each in its own collection
- **Quick build** — one click creates default sphere + cylinder reference objects
- **Bake Spring Physics** — writes location/rotation keyframes for every frame so rendering reads exact spring values directly from the depsgraph; matches viewport playback identically
- **Performance Mode** — hides Grease Pencil / Line Art objects and disables spring physics for smooth viewport playback; switch back to Render Mode before baking and rendering
- **Scene Line Art compatible** — crash-safe with Grease Pencil Scene Line Art modifier during rendering (Blender 5.0+)

---

## Installation

1. **Edit → Preferences → Add-ons → Install from Disk**
2. Select the downloaded zip-file
3. Enable it — panel appears in **View3D → Sidebar → Mannequin**

Requires Blender 4.0+.

---

## Workflow

1. Click **Quick Default Objects** (or assign your own head/body meshes)
2. Set **Z Offset** so the body aligns below the head
3. Press **+** to create a mannequin — a controller Empty, head, and body are created and grouped into a collection
4. Keyframe the **Empty controller** (move XYZ + rotate Z to steer)
5. Tune **Tilt** and **Spring** sliders to taste
6. Use **Reset Springs** after large timeline jumps
7. Enable **Performance Mode** (bottom of panel) while blocking out animation, then switch back to **Render Mode** before baking
8. Press **Bake Spring Physics** — this writes keyframes for every frame so the renderer reads them exactly
9. **Render Animation** — the live spring handler is automatically disabled during rendering; only baked keyframes drive the body

> **Re-bake** any time you change spring settings, tilt parameters, or the controller animation.

---

## Parameters

| Parameter | Description |
|---|---|
| **Tilt Scale** | How far the body leans during movement |
| **Max Tilt °** | Hard clamp on tilt angle (0–90°) |
| **Delay Frames** | How many frames behind the body reacts |
| **Stiffness** | Spring snap — high = snappy, low = lazy |
| **Damping** | Oscillation decay — ~1.0 = no bounce, ~0.3 = many swings |
| **Tilt ×** | Per-mannequin sensitivity multiplier |

---

## Rendering

Spring physics is a **stateful simulation** — the body rotation at frame N depends on the entire history of controller movement from frame 1. Blender's renderer does not guarantee that `frame_change_post` fires in strict sequential order for every frame, so a live handler cannot produce consistent results during rendering.

**How it works:**

- When rendering starts, the live `frame_change_post` handler is **automatically disabled**
- The renderer reads body transforms from **baked keyframes** via the normal depsgraph evaluation
- This gives frame-accurate, deterministic results with no handler-ordering or double-stepping issues

**Bake behaviour:**

The bake simulates the spring starting from a zero (rest) state at `frame_start`, which is the same initial condition as viewport playback when you press play from the beginning. The rendered output will match what you see when playing the animation in the viewport.

**Use Clear Bake** to remove the keyframes and return to live simulation for further tweaking. Always re-bake before the next render.

---

## Performance Mode

The add-on's `frame_change_post` handler updates the body object every frame, which triggers a depsgraph re-evaluation. When **Scene Line Art** is active this causes Line Art to re-process the scene geometry on every frame change, which is expensive even in Solid shading mode.

**Performance Mode** (button at the bottom of the panel) addresses this in two ways:

1. **Hides all Grease Pencil objects** from the viewport so Line Art is never evaluated during playback
2. **Disables spring physics** — the body only follows the controller position and Z rotation, with no tilt or oscillation. This eliminates the per-frame rotation change that triggers the most expensive depsgraph cascade

When you click **Exit Performance → Render Mode**, the Grease Pencil objects are restored and spring physics re-enabled. Spring state is reset so physics starts cleanly from the current frame.

> **Remember:** always switch back to Render Mode before hitting Render — otherwise Grease Pencil objects will be hidden in the output.

---

## Scene Optimisation Tips

These settings improve real-time playback performance for any scene using this add-on.

### Viewport settings

- **Solid shading** — use Solid mode while animating; Material Preview and Rendered viewport both evaluate shaders and lighting every frame
- **Disable viewport overlays** — turn off Overlays (the two-circle icon) to skip wireframe, stats, and other per-frame drawing passes
- **Disable Ambient Occlusion & Depth of Field in viewport** — both are expensive and not needed while blocking out movement (Viewport Shading → Options)
- **Reduce Clip End** — set the viewport Clip End distance as low as your scene allows; a smaller frustum reduces the number of objects Blender has to cull and sort each frame
- **Disable shadows in solid mode** — Viewport Shading → Lighting → MatCap or Flat removes real-time shadow calculation entirely

### Object & modifier settings

- **Keep proxy objects low-poly** — the add-on drives simple sphere + cylinder meshes by default; if you replace them with high-poly meshes every modifier stack on those objects is re-evaluated every frame
- **Reduce Bevel segments** — the default body has a Bevel modifier; lower segments (2 instead of 4) halve the vertex count at negligible visual cost during animation
- **Apply modifiers on static objects** — any object with a modifier that does not change during the animation should have its modifier applied (Ctrl+A → Apply All Modifiers) so Blender does not re-evaluate it on every frame change
- **Disable modifiers in viewport** — for any modifier not needed during animation, click the monitor icon in the modifier stack to hide it from the viewport without removing it
- **Avoid subdivision on animated objects** — Subdivision Surface on the body or head is re-evaluated every frame; use a Multires modifier or apply it instead

### Scene & render settings

- **Simplify** — enable Render → Simplify and set the Viewport subdivision level to 0 or 1 while animating; this globally caps subdivision on all objects
- **Limit Frame Rate** — in the Timeline header, set a lower target FPS (e.g. 12 or 15) if you only need to see rough timing; Blender drops frames but the playback cursor advances at real time
- **Use Frame Dropping** — Playback menu → Sync to Audio / Frame Dropping ensures the timeline keeps pace rather than waiting for slow frames
- **Disable Auto Keying when not needed** — Auto Keying writes to the action on every frame scrub, which can trigger unnecessary depsgraph updates
- **Hide unrelated objects** — objects hidden with `H` (hide in viewport) are excluded from the depsgraph evaluation pass for that viewport; hiding anything you are not currently working on reduces per-frame evaluation cost

### Grease Pencil / Line Art specific

- **Use Performance Mode while animating** — the single biggest win; see the section above
- **Reduce Line Art resolution** — in the Line Art modifier, lower the Edge Detection and Resolution settings; a lower resolution means fewer triangles to process in the spatial acceleration structure
- **Disable "Cache Lines" off-screen** — in the Line Art modifier, enable **Cache Lines** so the result is reused across frames where geometry has not changed
- **Use Object-level Line Art instead of Scene-level** — a Line Art modifier scoped to a single object is cheaper than Scene level, which processes every object in the scene
- **Separate Line Art into a linked scene** — use Blender's scene linking to put the Line Art Grease Pencil object in a separate scene and composite it over the main render; this way the Line Art scene can be rendered independently and cached
