bl_info = {
    "name": "Mannequin Follow Lag",
    "author": "alexejwsr",
    "version": (4, 0, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > Mannequin",
    "description": (
        "Animate an Empty controller — head and body follow with spring physics. "
        "Supports Z-rotation, curves, and multiple mannequins."
    ),
    "category": "Animation",
}

import bpy
import mathutils
import math

# ─────────────────────────────────────────────────────────────
#  Spring state  (keyed by item.name, reset on backward scrub)
#
#  "angle"      – current tilt angle (radians, signed scalar)
#  "ang_vel"    – angular velocity (rad / frame)
#  "last_frame" – last frame we updated on (for backward-scrub detection)
# ─────────────────────────────────────────────────────────────

_spring_state: dict = {}
_handler_running    = False
_is_rendering       = False        # True while a render job is active
_render_new_anim_objs: set = set() # body object names where we created animation data


def _get_state(key, scene):
    if key not in _spring_state:
        _spring_state[key] = {
            "angle":      0.0,
            "ang_vel":    0.0,
            "last_frame": scene.frame_current,
        }
    return _spring_state[key]


def _reset_state(key):
    _spring_state.pop(key, None)


# ─────────────────────────────────────────────────────────────
#  Velocity from world-position history
#
#  IMPORTANT: We deliberately avoid scene.frame_set() inside the
#  frame_change_post handler.  Calling frame_set() from that handler
#  triggers a full depsgraph re-evaluation on every sample, which
#  races with Blender's Line Art worker thread and causes a SIGSEGV
#  in lineart_bounding_area_link_triangle (near-null pointer dereference
#  into partially-built tile data structures).
#
#  Instead we store the controller's evaluated world position at each
#  frame in the spring state dict and derive velocity from that.
#  This works for every animation setup (direct keyframes, NLA, drivers,
#  constraints) and reads actual world-space coordinates.
# ─────────────────────────────────────────────────────────────

def _ctrl_velocity(ctrl_world_pos, state, frame, delay):
    """
    World-space XY velocity of the controller at (frame - delay),
    derived from the per-mannequin position history — no scene.frame_set().
    Returns Vector((vx, vy, 0)).

    History is keyed by frame number.  Central difference is used when
    both (t-1) and (t+1) are available; forward difference otherwise.
    """
    history = state.setdefault("pos_history", {})
    history[frame] = (ctrl_world_pos.x, ctrl_world_pos.y)

    # Prune entries no longer needed (keep delay + 2 frames of headroom)
    keep_from = frame - max(delay + 2, 3)
    for old_f in [k for k in list(history) if k < keep_from]:
        del history[old_f]

    t = frame - delay
    p_prev = history.get(t - 1)
    p_next = history.get(t + 1)
    p_cur  = history.get(t)

    if p_prev is not None and p_next is not None:
        return mathutils.Vector(((p_next[0] - p_prev[0]) * 0.5,
                                 (p_next[1] - p_prev[1]) * 0.5, 0.0))
    if p_prev is not None and p_cur is not None:
        return mathutils.Vector((p_cur[0] - p_prev[0],
                                 p_cur[1] - p_prev[1], 0.0))
    return mathutils.Vector((0.0, 0.0, 0.0))


# ─────────────────────────────────────────────────────────────
#  Spring integrator  (semi-implicit Euler)
#
#  We simulate a *scalar* angle spring.  The tilt axis is computed
#  fresh every frame from the velocity direction + controller Z rotation,
#  so it naturally follows curves and direction changes without any
#  axis-blending artefacts.
#
#  target_angle:
#    - moving   → negative (lean back against travel direction)
#    - stopped  → 0  (spring oscillates through upright and settles)
# ─────────────────────────────────────────────────────────────

def _spring_step(state, target_angle, stiffness, damping):
    angle   = state["angle"]
    ang_vel = state["ang_vel"]

    accel   = -stiffness * (angle - target_angle) - damping * ang_vel

    # Semi-implicit Euler (velocity first, then position)
    ang_vel = ang_vel + accel
    angle   = angle   + ang_vel

    state["angle"]   = angle
    state["ang_vel"] = ang_vel
    return angle


# ─────────────────────────────────────────────────────────────
#  Build the world-space tilt axis purely from movement direction.
#
#  The controller's Z rotation (head facing) is intentionally ignored.
#  Tilt is always opposite to the direction of travel in world space,
#  so a character can move sideways or backwards without the tilt
#  snapping to the head orientation.
# ─────────────────────────────────────────────────────────────

def _tilt_axis_world(vel_world):
    """
    Returns the world-space unit vector around which the body tilts.
    The axis is 90° perpendicular to the XY velocity (cross with +Z),
    so the body leans directly opposite the travel direction regardless
    of which way the head/controller is rotated.
    """
    speed_2d = math.sqrt(vel_world.x ** 2 + vel_world.y ** 2)
    if speed_2d < 0.00001:
        return None

    # Normalised travel direction in world XY
    fx = vel_world.x / speed_2d
    fy = vel_world.y / speed_2d

    # Tilt axis = 90° CCW of travel direction (cross of travel with +Z)
    # cross(travel, +Z) = (fy*1 - 0, 0 - fx*1, 0) = (fy, -fx, 0)
    return mathutils.Vector((-fy, fx, 0.0))


# ─────────────────────────────────────────────────────────────
#  Per-mannequin update
# ─────────────────────────────────────────────────────────────

def _update_mannequin(item, scene, props):
    ctrl_obj = item.ctrl_object
    head_obj = item.head_object
    body_obj = item.body_object

    if ctrl_obj is None or head_obj is None or body_obj is None:
        return

    cur   = scene.frame_current
    key   = item.name
    state = _get_state(key, scene)

    # ── Backward scrub → reset spring ──
    if cur < state["last_frame"]:
        _reset_state(key)
        state = _get_state(key, scene)
    state["last_frame"] = cur

    # ── Read controller state at current frame directly from the evaluated matrix.
    #    No scene.frame_set() — we are already at `cur` inside frame_change_post.
    ctrl_mat       = ctrl_obj.matrix_world
    ctrl_world_pos = ctrl_mat.to_translation()
    ctrl_z         = ctrl_mat.to_euler('XYZ').z

    # ── Head: match controller XYZ position exactly, keep its own Z rotation ──
    # The head is parented to the ctrl Empty, so we only need to ensure
    # its world position/rotation track the ctrl.  If parented it moves
    # automatically; we still set world location for the body calculation.
    head_world = head_obj.matrix_world.to_translation()

    # ── Body: position = head XY, Z = head - offset; Z rotation = controller ──
    new_loc = mathutils.Vector((
        head_world.x,
        head_world.y,
        head_world.z - item.z_offset,
    ))
    body_obj.location = new_loc

    # ── Performance mode: skip spring/tilt for smooth viewport playback ──
    # Only location and Z rotation are updated, keeping depsgraph updates
    # minimal and avoiding the per-frame Line Art evaluation cascade.
    if props.preview_mode:
        body_obj.rotation_mode = 'QUATERNION'
        q = mathutils.Quaternion(mathutils.Vector((0.0, 0.0, 1.0)), ctrl_z)
        body_obj.rotation_quaternion = q
        # Cache transforms so render_pre can re-apply without re-advancing physics.
        state["physics_frame"]  = cur
        state["final_location"] = new_loc.copy()
        state["final_rotation"] = q.copy()
        if _is_rendering:
            _try_insert_render_keyframe(body_obj, cur)
        return

    # ── Controller velocity (delayed) ──
    vel   = _ctrl_velocity(ctrl_world_pos, state, cur, props.delay_frames)
    speed = vel.length

    max_tilt = math.radians(props.max_tilt_degrees)

    # ── Target tilt angle ──
    if speed > 0.00001:
        raw_target   = +(speed * item.sensitivity * props.counter_rotation_scale)
        target_angle = max(-max_tilt, min(max_tilt, raw_target))
        tilt_axis    = _tilt_axis_world(vel)
        if tilt_axis is not None:
            state["last_axis"] = tilt_axis   # persist for oscillation after stop
    else:
        target_angle = 0.0
        tilt_axis    = None   # will reuse stored axis below

    # ── Advance spring (scalar) ──
    angle = _spring_step(
        state,
        target_angle,
        stiffness = props.spring_stiffness,
        damping   = props.spring_damping,
    )
    angle = max(-max_tilt, min(max_tilt, angle))

    # ── Compose body rotation:
    #    1. Z rotation = controller Z (character facing direction)
    #    2. Tilt = spring angle around the velocity-perpendicular axis
    # ──
    body_obj.rotation_mode = 'QUATERNION'

    # Base Z rotation quaternion
    z_up  = mathutils.Vector((0.0, 0.0, 1.0))
    q_z   = mathutils.Quaternion(z_up, ctrl_z)

    # Tilt quaternion (only if we have a meaningful axis)
    if tilt_axis is not None and abs(angle) > 0.0001:
        q_tilt = mathutils.Quaternion(tilt_axis, angle)
    elif tilt_axis is None and abs(angle) > 0.0001:
        # Stopped but spring still oscillating — reuse last stored world axis
        last_axis = state.get("last_axis", mathutils.Vector((1.0, 0.0, 0.0)))
        q_tilt = mathutils.Quaternion(last_axis, angle)
    else:
        q_tilt = mathutils.Quaternion()   # identity

    final_q = q_tilt @ q_z
    body_obj.rotation_quaternion = final_q

    # Cache transforms so render_pre can re-apply without re-advancing physics.
    state["physics_frame"]  = cur
    state["final_location"] = new_loc.copy()
    state["final_rotation"] = final_q.copy()
    if _is_rendering:
        _try_insert_render_keyframe(body_obj, cur)


# ─────────────────────────────────────────────────────────────
#  Frame-change handler
# ─────────────────────────────────────────────────────────────

def mannequin_handler(scene):
    global _handler_running
    if _handler_running:
        return
    _handler_running = True
    try:
        mlist = scene.mannequin_list
        if not mlist:
            return
        props = scene.mannequin_props
        for item in mlist:
            _update_mannequin(item, scene, props)
    finally:
        _handler_running = False


def _register_handler():
    if mannequin_handler not in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.append(mannequin_handler)


def _unregister_handler():
    if mannequin_handler in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.remove(mannequin_handler)


# ─────────────────────────────────────────────────────────────
#  Render-pre handler
#
#  Problem: during animation rendering Blender may perform an additional
#  depsgraph evaluation *after* frame_change_post fires (to resolve
#  constraints and drivers for the render engine). This extra evaluation
#  can overwrite the body-object transforms we set in mannequin_handler,
#  causing the renderer to capture the un-sprung default rotation instead
#  of the spring-physics result — even though the viewport looks correct.
#
#  Fix: register a render_pre handler that re-applies the already-computed
#  transforms (cached in _spring_state by _update_mannequin) immediately
#  before the render engine captures each frame.  Because the transforms
#  are read from the cache rather than re-simulated, the spring integrator
#  is NOT stepped a second time, so the physics remain frame-accurate.
# ─────────────────────────────────────────────────────────────

@bpy.app.handlers.persistent
def mannequin_render_pre(scene, depsgraph=None):
    """Re-apply cached spring transforms before each rendered frame.

    Fires once per rendered frame (including every frame of an animation
    render). If frame_change_post already ran for this frame the cached
    location/rotation is simply rewritten to the body object; if for any
    reason it has not yet run, _update_mannequin is called normally so
    that the frame is never silently skipped.
    """
    global _handler_running
    if _handler_running:
        return
    _handler_running = True
    try:
        mlist = scene.mannequin_list
        if not mlist:
            return
        props = scene.mannequin_props
        cur   = scene.frame_current
        for item in mlist:
            key      = item.name
            state    = _spring_state.get(key)
            body_obj = item.body_object
            if body_obj is None:
                continue

            if (state is not None
                    and state.get("physics_frame") == cur
                    and "final_location" in state
                    and "final_rotation" in state):
                # frame_change_post already computed & cached the result —
                # just rewrite the body transforms so the renderer sees them.
                body_obj.rotation_mode       = 'QUATERNION'
                body_obj.location            = state["final_location"].copy()
                body_obj.rotation_quaternion = state["final_rotation"].copy()
                # Also insert a keyframe so the depsgraph evaluation for
                # rendering picks up our spring transforms (e.g. single-frame
                # renders where frame_change_post hasn't fired for this frame).
                _try_insert_render_keyframe(body_obj, cur)
            else:
                # frame_change_post has not run yet for this frame; compute now.
                # _update_mannequin handles keyframe insertion when _is_rendering.
                _update_mannequin(item, scene, props)
    finally:
        _handler_running = False


def _try_insert_render_keyframe(body_obj, cur):
    """Insert location/rotation keyframes during rendering so Blender's evaluated
    depsgraph uses our spring-computed transforms rather than stored defaults.

    Only acts when body_obj had no pre-existing action (to avoid disturbing
    user-authored animation). Tracks which objects received new animation data
    so _cleanup_render_bake can remove it afterwards.
    """
    name     = body_obj.name
    had_anim = (body_obj.animation_data is not None
                and body_obj.animation_data.action is not None)
    if had_anim and name not in _render_new_anim_objs:
        return  # body has user animation — don't interfere
    if not had_anim:
        _render_new_anim_objs.add(name)
    body_obj.keyframe_insert(data_path='location',            frame=cur)
    body_obj.keyframe_insert(data_path='rotation_quaternion', frame=cur)


def _cleanup_render_bake(scene):
    """Remove temporary animation data inserted during rendering."""
    global _render_new_anim_objs
    for item in scene.mannequin_list:
        body_obj = item.body_object
        if body_obj is not None and body_obj.name in _render_new_anim_objs:
            body_obj.animation_data_clear()
    _render_new_anim_objs = set()


def _register_render_handler():
    if mannequin_render_pre not in bpy.app.handlers.render_pre:
        bpy.app.handlers.render_pre.append(mannequin_render_pre)


def _unregister_render_handler():
    if mannequin_render_pre in bpy.app.handlers.render_pre:
        bpy.app.handlers.render_pre.remove(mannequin_render_pre)


# ─────────────────────────────────────────────────────────────
#  Render lifecycle handlers
#
#  render_init  → set _is_rendering so _update_mannequin inserts keyframes.
#  render_complete / render_cancel → clean up those keyframes and re-run
#  the spring handler so the viewport immediately shows the correct pose.
# ─────────────────────────────────────────────────────────────

@bpy.app.handlers.persistent
def _render_init_handler(scene, depsgraph=None):
    global _is_rendering
    _is_rendering = True


@bpy.app.handlers.persistent
def _render_complete_handler(scene, depsgraph=None):
    global _is_rendering
    _is_rendering = False
    _cleanup_render_bake(scene)
    mannequin_handler(scene)


@bpy.app.handlers.persistent
def _render_cancel_handler(scene, depsgraph=None):
    global _is_rendering
    _is_rendering = False
    _cleanup_render_bake(scene)
    mannequin_handler(scene)


def _register_render_state_handlers():
    if _render_init_handler not in bpy.app.handlers.render_init:
        bpy.app.handlers.render_init.append(_render_init_handler)
    if _render_complete_handler not in bpy.app.handlers.render_complete:
        bpy.app.handlers.render_complete.append(_render_complete_handler)
    if _render_cancel_handler not in bpy.app.handlers.render_cancel:
        bpy.app.handlers.render_cancel.append(_render_cancel_handler)


def _unregister_render_state_handlers():
    for handler, handler_list in [
        (_render_init_handler,     bpy.app.handlers.render_init),
        (_render_complete_handler, bpy.app.handlers.render_complete),
        (_render_cancel_handler,   bpy.app.handlers.render_cancel),
    ]:
        if handler in handler_list:
            handler_list.remove(handler)


# ─────────────────────────────────────────────────────────────
#  Properties
# ─────────────────────────────────────────────────────────────

def _on_global_change(self, context):
    _spring_state.clear()
    mannequin_handler(context.scene)


class MannequinItem(bpy.types.PropertyGroup):
    name:        bpy.props.StringProperty(name="Name", default="Mannequin")
    ctrl_object: bpy.props.PointerProperty(
        name="Controller (Empty)",
        type=bpy.types.Object,
        description="The Empty you animate — drives position and Z rotation",
    )
    head_object: bpy.props.PointerProperty(
        name="Head",
        type=bpy.types.Object,
        description="Head mesh (parented to Controller)",
    )
    body_object: bpy.props.PointerProperty(
        name="Body",
        type=bpy.types.Object,
        description="Body mesh — driven by the addon",
    )
    z_offset: bpy.props.FloatProperty(
        name="Z Offset",
        description="Distance from head center down to body center",
        default=0.65, min=0.0, max=10.0, precision=3,
    )
    sensitivity: bpy.props.FloatProperty(
        name="Tilt ×",
        description="Per-mannequin tilt sensitivity",
        default=1.0, min=0.0, max=10.0, precision=2,
    )


class MannequinProperties(bpy.types.PropertyGroup):
    # ── Reference ──
    ref_head: bpy.props.PointerProperty(name="Reference Head", type=bpy.types.Object)
    ref_body: bpy.props.PointerProperty(name="Reference Body", type=bpy.types.Object)
    ref_z_offset: bpy.props.FloatProperty(
        name="Z Offset", default=0.65, min=0.0, max=10.0, precision=3,
    )

    # ── Tilt ──
    counter_rotation_scale: bpy.props.FloatProperty(
        name="Tilt Scale",
        description="How far the body leans during movement",
        default=3.0, min=0.0, max=20.0, step=10, precision=2,
        update=_on_global_change,
    )
    max_tilt_degrees: bpy.props.FloatProperty(
        name="Max Tilt °",
        description="Hard clamp on tilt angle",
        default=35.0, min=0.0, max=90.0, step=100,
        update=_on_global_change,
    )
    delay_frames: bpy.props.IntProperty(
        name="Delay Frames",
        description="How many frames behind the body reacts",
        default=0, min=0, max=30,
        update=_on_global_change,
    )

    # ── Spring ──
    spring_stiffness: bpy.props.FloatProperty(
        name="Stiffness",
        description="How quickly the spring returns to target. High=snappy, low=lazy",
        default=0.25, min=0.01, max=2.0, step=1, precision=3,
        update=_on_global_change,
    )
    spring_damping: bpy.props.FloatProperty(
        name="Damping",
        description="How fast oscillations die. ~1.0=no bounce, ~0.3=several swings",
        default=0.35, min=0.01, max=2.0, step=1, precision=3,
        update=_on_global_change,
    )

    active_index: bpy.props.IntProperty(default=0)

    # ── Performance mode (hides GP/Line-Art objects, disables spring for fast playback) ──
    preview_mode: bpy.props.BoolProperty(
        name="Performance Mode",
        description=(
            "Hides Grease Pencil objects and disables spring physics for "
            "smooth viewport playback. Switch back to Render Mode before rendering."
        ),
        default=False,
    )
    hidden_gp_objects: bpy.props.StringProperty(
        name="Hidden GP Objects",
        description="Newline-separated names of objects hidden by Performance Mode",
        default="",
    )


# ─────────────────────────────────────────────────────────────
#  Operators
# ─────────────────────────────────────────────────────────────

class MANNEQUIN_OT_quick_build(bpy.types.Operator):
    """Create simple sphere + cylinder reference objects."""
    bl_idname  = "mannequin.quick_build"
    bl_label   = "Quick Default Objects"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.mannequin_props

        bpy.ops.mesh.primitive_uv_sphere_add(radius=0.20, location=(0, 0, 1.70))
        head      = context.active_object
        head.name = "MannequinRef_Head"

        bpy.ops.mesh.primitive_cylinder_add(radius=0.18, depth=0.70, location=(0, 0, 1.05))
        body      = context.active_object
        body.name = "MannequinRef_Body"
        bev          = body.modifiers.new("Bevel", 'BEVEL')
        bev.width    = 0.08
        bev.segments = 4

        props.ref_head     = head
        props.ref_body     = body
        props.ref_z_offset = 0.65
        self.report({'INFO'}, "Reference objects created. Assign them, then click +.")
        return {'FINISHED'}


class MANNEQUIN_OT_create(bpy.types.Operator):
    """
    Duplicate reference head + body, create a controller Empty,
    parent head to Empty, register as a new mannequin.
    """
    bl_idname  = "mannequin.create"
    bl_label   = "Add Mannequin"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        props = scene.mannequin_props

        if props.ref_head is None or props.ref_body is None:
            self.report({'ERROR'}, "Set Reference Head and Reference Body first.")
            return {'CANCELLED'}

        idx    = len(scene.mannequin_list)
        offset = mathutils.Vector((idx * 1.5, 0.0, 0.0))

        # ── Duplicate head ──
        bpy.ops.object.select_all(action='DESELECT')
        props.ref_head.select_set(True)
        context.view_layer.objects.active = props.ref_head
        bpy.ops.object.duplicate(linked=False)
        new_head           = context.active_object
        new_head.name      = f"Mannequin_{idx:02d}_Head"
        new_head.location += offset

        # ── Duplicate body ──
        bpy.ops.object.select_all(action='DESELECT')
        props.ref_body.select_set(True)
        context.view_layer.objects.active = props.ref_body
        bpy.ops.object.duplicate(linked=False)
        new_body      = context.active_object
        new_body.name = f"Mannequin_{idx:02d}_Body"

        h_loc = new_head.location.copy()
        new_body.location = mathutils.Vector((
            h_loc.x, h_loc.y, h_loc.z - props.ref_z_offset,
        ))

        # ── Set body origin to head center (rotation anchor) ──
        saved_cursor          = scene.cursor.location.copy()
        scene.cursor.location = h_loc
        bpy.ops.object.select_all(action='DESELECT')
        new_body.select_set(True)
        context.view_layer.objects.active = new_body
        bpy.ops.object.origin_set(type='ORIGIN_CURSOR')
        scene.cursor.location = saved_cursor

        # ── Create controller Empty at head position ──
        bpy.ops.object.empty_add(type='PLAIN_AXES', location=h_loc)
        ctrl           = context.active_object
        ctrl.name      = f"Mannequin_{idx:02d}_Ctrl"
        ctrl.empty_display_size = 0.3

        # ── Parent head to controller (keep transforms) ──
        bpy.ops.object.select_all(action='DESELECT')
        new_head.select_set(True)
        ctrl.select_set(True)
        context.view_layer.objects.active = ctrl
        bpy.ops.object.parent_set(type='OBJECT', keep_transform=True)

        # ── Create a dedicated collection for this mannequin ──
        col_name = f"Mannequin_{idx:02d}"
        col = bpy.data.collections.new(col_name)
        scene.collection.children.link(col)

        # Move all three objects into the new collection
        # (remove from whichever collections they currently live in first)
        for ob in (ctrl, new_head, new_body):
            for old_col in list(ob.users_collection):
                old_col.objects.unlink(ob)
            col.objects.link(ob)

        # ── Register ──
        item             = scene.mannequin_list.add()
        item.name        = ctrl.name
        item.ctrl_object = ctrl
        item.head_object = new_head
        item.body_object = new_body
        item.z_offset    = props.ref_z_offset
        item.sensitivity = 1.0

        props.active_index = len(scene.mannequin_list) - 1
        self.report({'INFO'},
            f"Created {ctrl.name} in collection '{col_name}'  ·  Animate the Empty.")
        return {'FINISHED'}


class MANNEQUIN_OT_remove(bpy.types.Operator):
    """Remove the selected mannequin from the list."""
    bl_idname  = "mannequin.remove"
    bl_label   = "Remove Mannequin"
    bl_options = {'REGISTER', 'UNDO'}

    delete_objects: bpy.props.BoolProperty(
        name="Also delete objects from scene", default=False,
    )

    def execute(self, context):
        scene = context.scene
        props = scene.mannequin_props
        mlist = scene.mannequin_list
        idx   = props.active_index

        if not (0 <= idx < len(mlist)):
            self.report({'WARNING'}, "No mannequin selected.")
            return {'CANCELLED'}

        item = mlist[idx]
        _reset_state(item.name)

        if self.delete_objects:
            for ob in (item.ctrl_object, item.head_object, item.body_object):
                if ob:
                    bpy.data.objects.remove(ob, do_unlink=True)

        mlist.remove(idx)
        props.active_index = max(0, idx - 1)
        return {'FINISHED'}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        self.layout.prop(self, "delete_objects")


class MANNEQUIN_OT_toggle_preview(bpy.types.Operator):
    """
    Performance Mode: hides all Grease Pencil objects and disables spring
    physics for smooth viewport playback. Switch back to Render Mode before
    rendering to restore Line Art and full spring simulation.
    """
    bl_idname  = "mannequin.toggle_preview"
    bl_label   = "Toggle Performance Mode"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.mannequin_props

        if not props.preview_mode:
            # ── Enter preview mode: hide all visible GP objects ──
            hidden = []
            for obj in context.scene.objects:
                if obj.type in ('GREASEPENCIL', 'GPENCIL') and not obj.hide_viewport:
                    obj.hide_viewport = True
                    hidden.append(obj.name)
            props.hidden_gp_objects = "\n".join(hidden)
            props.preview_mode = True
            self.report({'INFO'},
                f"Performance mode ON — hid {len(hidden)} Grease Pencil object(s), "
                "spring physics disabled. Switch back before rendering.")
        else:
            # ── Exit preview mode: restore previously hidden GP objects ──
            restored = 0
            for name in props.hidden_gp_objects.split("\n"):
                name = name.strip()
                if name and name in bpy.data.objects:
                    bpy.data.objects[name].hide_viewport = False
                    restored += 1
            props.hidden_gp_objects = ""
            props.preview_mode = False
            # Reset spring state so physics starts clean from the current frame
            _spring_state.clear()
            self.report({'INFO'},
                f"Render mode ON — restored {restored} Grease Pencil object(s), "
                "spring physics re-enabled.")

        return {'FINISHED'}


class MANNEQUIN_OT_reset_springs(bpy.types.Operator):
    """Clear all spring states (use after big timeline jumps)."""
    bl_idname = "mannequin.reset_springs"
    bl_label  = "Reset Springs"

    def execute(self, context):
        _spring_state.clear()
        mannequin_handler(context.scene)
        self.report({'INFO'}, "Spring states cleared.")
        return {'FINISHED'}


class MANNEQUIN_OT_refresh(bpy.types.Operator):
    """Force-update all mannequin bodies at the current frame."""
    bl_idname = "mannequin.refresh"
    bl_label  = "Refresh Now"

    def execute(self, context):
        mannequin_handler(context.scene)
        for area in context.screen.areas:
            area.tag_redraw()
        return {'FINISHED'}


# ─────────────────────────────────────────────────────────────
#  UI List
# ─────────────────────────────────────────────────────────────

class MANNEQUIN_UL_list(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_propname):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.label(text=item.name, icon='EMPTY_ARROWS')
            row.prop(item, "sensitivity", text="Tilt×", emboss=False)
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="", icon='EMPTY_ARROWS')


# ─────────────────────────────────────────────────────────────
#  Panel
# ─────────────────────────────────────────────────────────────

class MANNEQUIN_PT_panel(bpy.types.Panel):
    bl_label       = "Mannequin Follow"
    bl_idname      = "MANNEQUIN_PT_panel"
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category    = "Mannequin"

    def draw(self, context):
        layout = self.layout
        scene  = context.scene
        props  = scene.mannequin_props
        mlist  = scene.mannequin_list

        # ── Reference ──
        box = layout.box()
        box.label(text="Reference Objects", icon='OBJECT_DATA')
        box.prop(props, "ref_head", icon='MESH_UVSPHERE')
        box.prop(props, "ref_body", icon='MESH_CYLINDER')
        box.prop(props, "ref_z_offset")
        box.operator("mannequin.quick_build", icon='MODIFIER')

        layout.separator()

        # ── Mannequin list ──
        box = layout.box()
        box.label(text="Mannequins", icon='EMPTY_ARROWS')
        row = box.row()
        row.template_list(
            "MANNEQUIN_UL_list", "",
            scene, "mannequin_list",
            props, "active_index",
            rows=4,
        )
        col = row.column(align=True)
        col.operator("mannequin.create", icon='ADD',    text="")
        col.operator("mannequin.remove", icon='REMOVE', text="")

        if 0 <= props.active_index < len(mlist):
            item = mlist[props.active_index]
            sub  = box.box()
            sub.label(text=item.name, icon='SETTINGS')
            sub.prop(item, "ctrl_object")
            sub.prop(item, "head_object")
            sub.prop(item, "body_object")
            sub.prop(item, "z_offset",    slider=False)
            sub.prop(item, "sensitivity", slider=True)

        layout.separator()

        # ── Global Tuning ──
        box = layout.box()
        box.label(text="Global Tuning", icon='PREFERENCES')

        sub = box.box()
        sub.label(text="Tilt", icon='ORIENTATION_GIMBAL')
        sub.prop(props, "counter_rotation_scale", slider=True)
        sub.prop(props, "max_tilt_degrees",        slider=True)
        sub.prop(props, "delay_frames",            slider=True)

        sub = box.box()
        sub.label(text="Spring / Oscillation", icon='MOD_PHYSICS')
        sub.prop(props, "spring_stiffness", slider=True)
        sub.prop(props, "spring_damping",   slider=True)

        row = box.row(align=True)
        row.operator("mannequin.refresh",       icon='FILE_REFRESH')
        row.operator("mannequin.reset_springs", icon='LOOP_BACK')

        layout.separator()

        # ── Hint ──
        box = layout.box()
        col = box.column(align=True)
        col.scale_y = 0.8
        col.label(text="Workflow:", icon='INFO')
        col.label(text="1. Assign Ref Head + Body (or Quick Default)")
        col.label(text="2. Set Z Offset, press + to add mannequins")
        col.label(text="3. Animate the EMPTY controller (arrows icon)")
        col.label(text="   — move XYZ + rotate Z to steer the character")
        col.label(text="4. Tune Tilt + Spring sliders globally")
        col.label(text="5. Reset Springs after big timeline jumps")

        layout.separator()

        # ── Preview / Render mode toggle ──
        row = layout.row(align=True)
        row.alert = props.preview_mode
        if props.preview_mode:
            row.operator("mannequin.toggle_preview",
                         text="Exit Performance  →  Render Mode",
                         icon='RESTRICT_VIEW_ON')
        else:
            row.operator("mannequin.toggle_preview",
                         text="Performance Mode  (No Spring / Line Art)",
                         icon='RESTRICT_VIEW_OFF')


# ─────────────────────────────────────────────────────────────
#  Persistent re-registration on file load
# ─────────────────────────────────────────────────────────────

@bpy.app.handlers.persistent
def _load_post_handler(dummy):
    _spring_state.clear()
    _register_handler()
    _register_render_handler()
    _register_render_state_handlers()


# ─────────────────────────────────────────────────────────────
#  Register / Unregister
# ─────────────────────────────────────────────────────────────

classes = (
    MannequinItem,
    MannequinProperties,
    MANNEQUIN_UL_list,
    MANNEQUIN_OT_quick_build,
    MANNEQUIN_OT_create,
    MANNEQUIN_OT_remove,
    MANNEQUIN_OT_toggle_preview,
    MANNEQUIN_OT_reset_springs,
    MANNEQUIN_OT_refresh,
    MANNEQUIN_PT_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.mannequin_props = bpy.props.PointerProperty(type=MannequinProperties)
    bpy.types.Scene.mannequin_list  = bpy.props.CollectionProperty(type=MannequinItem)
    _register_handler()
    _register_render_handler()
    _register_render_state_handlers()
    bpy.app.handlers.load_post.append(_load_post_handler)


def unregister():
    _unregister_handler()
    _unregister_render_handler()
    _unregister_render_state_handlers()
    if _load_post_handler in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_load_post_handler)
    del bpy.types.Scene.mannequin_list
    del bpy.types.Scene.mannequin_props
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
