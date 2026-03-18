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
