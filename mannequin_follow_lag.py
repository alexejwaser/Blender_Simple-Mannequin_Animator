bl_info = {
    "name": "Mannequin Follow Lag",
    "author": "Custom",
    "version": (3, 0, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > Mannequin",
    "description": (
        "Body follows head with spring physics: tilt, overshoot, damped oscillation. "
        "Live via frame-change handler. Supports multiple mannequins."
    ),
    "category": "Animation",
}

import bpy
import mathutils
import math

# ─────────────────────────────────────────────────────────────
#  Spring state  (stored per mannequin, reset on backward scrub)
#
#  Each entry keyed by item.name:
#    "angle"      – current tilt angle (radians, signed)
#    "velocity"   – angular velocity (rad/frame)
#    "axis"       – last non-zero tilt axis (Vector3)
#    "last_frame" – frame we last updated on
# ─────────────────────────────────────────────────────────────

_spring_state: dict = {}
_handler_running    = False


def _get_state(key, scene):
    """Return spring state dict for this key, initialising if missing."""
    if key not in _spring_state:
        _spring_state[key] = {
            "angle":      0.0,
            "velocity":   0.0,
            "axis":       mathutils.Vector((1.0, 0.0, 0.0)),
            "last_frame": scene.frame_current,
        }
    return _spring_state[key]


def _reset_state(key):
    if key in _spring_state:
        del _spring_state[key]


# ─────────────────────────────────────────────────────────────
#  Head sampling helpers
# ─────────────────────────────────────────────────────────────

def _sample_pos_xy(obj, scene, frame):
    """World XY position of obj at frame (clamped to range)."""
    f = max(scene.frame_start, min(scene.frame_end, frame))
    scene.frame_set(f)
    p = obj.matrix_world.to_translation()
    return mathutils.Vector((p.x, p.y, 0.0))


def _head_velocity_at(obj, scene, frame, delay):
    """XY velocity vector at (frame - delay) via central difference."""
    t       = frame - delay
    p_prev  = _sample_pos_xy(obj, scene, t - 1)
    p_next  = _sample_pos_xy(obj, scene, t + 1)
    scene.frame_set(frame)          # restore
    return (p_next - p_prev) * 0.5


# ─────────────────────────────────────────────────────────────
#  Spring step  (semi-implicit Euler, one frame at a time)
#
#  The spring equation:
#      angle_accel = -stiffness * (angle - target) - damping * ang_vel
#
#  target_angle is determined by current head speed:
#      - moving  → lean BACK  (negative angle by convention)
#      - stopped → 0
#  When the head decelerates hard, target suddenly flips toward 0 (or past 0),
#  the spring overshoots and oscillates around 0 naturally.
# ─────────────────────────────────────────────────────────────

def _spring_step(state, target_angle, target_axis,
                 stiffness, damping, dt=1.0):
    """
    Advance the spring by dt frames.
    Returns (new_angle, new_axis).
    """
    angle = state["angle"]
    vel   = state["velocity"]
    axis  = state["axis"]

    # Blend axis smoothly so direction changes don't snap
    # (only blend when the new axis is meaningful)
    if target_axis.length > 0.5:
        axis = axis.lerp(target_axis, 0.25).normalized()

    # Spring force toward target
    spring_force  = -stiffness * (angle - target_angle)
    damping_force = -damping   * vel
    accel         = spring_force + damping_force

    # Semi-implicit Euler
    vel   = vel   + accel * dt
    angle = angle + vel   * dt

    state["angle"]    = angle
    state["velocity"] = vel
    state["axis"]     = axis

    return angle, axis


# ─────────────────────────────────────────────────────────────
#  Per-mannequin update
# ─────────────────────────────────────────────────────────────

def _update_mannequin(item, scene, props):
    head_obj = item.head_object
    body_obj = item.body_object
    if head_obj is None or body_obj is None:
        return

    cur   = scene.frame_current
    key   = item.name
    state = _get_state(key, scene)

    # ── Detect backward scrub → reset spring ──
    if cur < state["last_frame"]:
        _reset_state(key)
        state = _get_state(key, scene)
    state["last_frame"] = cur

    # ── Position: XY = head, Z = head - offset ──
    h_world = head_obj.matrix_world.to_translation()
    body_obj.location = mathutils.Vector((
        h_world.x,
        h_world.y,
        h_world.z - item.z_offset,
    ))

    # ── Head velocity (delayed) ──
    vel   = _head_velocity_at(head_obj, scene, cur, props.delay_frames)
    speed = vel.length

    z_up = mathutils.Vector((0.0, 0.0, 1.0))

    # ── Target tilt angle ──
    # Moving → lean back (negative).  Stopped → 0 (spring will oscillate).
    max_tilt = math.radians(props.max_tilt_degrees)

    if speed > 0.00001:
        vel_dir      = vel.normalized()
        target_axis  = vel_dir.cross(z_up).normalized()
        raw_target   = -(speed * item.sensitivity * props.counter_rotation_scale)
        target_angle = max(-max_tilt, min(max_tilt, raw_target))
    else:
        target_axis  = state["axis"]   # keep last axis, target = 0
        target_angle = 0.0

    # ── Advance spring ──
    angle, axis = _spring_step(
        state,
        target_angle,
        target_axis,
        stiffness = props.spring_stiffness,
        damping   = props.spring_damping,
    )

    # Clamp to max_tilt (hard stop, doesn't kill velocity)
    angle = max(-max_tilt, min(max_tilt, angle))

    # ── Apply rotation ──
    body_obj.rotation_mode       = 'QUATERNION'
    body_obj.rotation_quaternion = mathutils.Quaternion(axis, angle)


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
#  Properties
# ─────────────────────────────────────────────────────────────

def _on_global_change(self, context):
    # Reset all spring states so changes feel immediate
    _spring_state.clear()
    mannequin_handler(context.scene)


class MannequinItem(bpy.types.PropertyGroup):
    name:        bpy.props.StringProperty(name="Name", default="Mannequin")
    head_object: bpy.props.PointerProperty(name="Head", type=bpy.types.Object)
    body_object: bpy.props.PointerProperty(name="Body", type=bpy.types.Object)
    z_offset:    bpy.props.FloatProperty(
        name="Z Offset",
        description="Distance from head center down to body center",
        default=0.65, min=0.0, max=10.0, precision=3,
    )
    sensitivity: bpy.props.FloatProperty(
        name="Tilt ×",
        description="Per-mannequin tilt sensitivity multiplier",
        default=1.0, min=0.0, max=10.0, precision=2,
    )


class MannequinProperties(bpy.types.PropertyGroup):
    # ── Reference ──
    ref_head: bpy.props.PointerProperty(
        name="Reference Head", type=bpy.types.Object,
    )
    ref_body: bpy.props.PointerProperty(
        name="Reference Body", type=bpy.types.Object,
    )
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
        description=(
            "How quickly the spring pulls back to the target angle. "
            "High = snappy, low = slow/lazy"
        ),
        default=0.25, min=0.01, max=2.0, step=1, precision=3,
        update=_on_global_change,
    )
    spring_damping: bpy.props.FloatProperty(
        name="Damping",
        description=(
            "How fast oscillations die out. "
            "1.0 = critically damped (no bounce), "
            "< 0.3 = many swings, "
            "> 1.0 = over-damped (slow creep)"
        ),
        default=0.35, min=0.01, max=2.0, step=1, precision=3,
        update=_on_global_change,
    )

    active_index: bpy.props.IntProperty(default=0)


# ─────────────────────────────────────────────────────────────
#  Operators
# ─────────────────────────────────────────────────────────────

class MANNEQUIN_OT_quick_build(bpy.types.Operator):
    """Create simple sphere + cylinder reference objects."""
    bl_idname  = "mannequin.quick_build"
    bl_label   = "Quick Default Objects"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        props = scene.mannequin_props

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
    """Duplicate reference objects and register as a new mannequin."""
    bl_idname  = "mannequin.create"
    bl_label   = "Add Mannequin"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        props = scene.mannequin_props

        if props.ref_head is None or props.ref_body is None:
            self.report({'ERROR'}, "Set Reference Head and Reference Body first.")
            return {'CANCELLED'}

        idx = len(scene.mannequin_list)

        bpy.ops.object.select_all(action='DESELECT')
        props.ref_head.select_set(True)
        context.view_layer.objects.active = props.ref_head
        bpy.ops.object.duplicate(linked=False)
        new_head      = context.active_object
        new_head.name = f"Mannequin_{idx:02d}_Head"
        new_head.location.x += idx * 1.0

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

        saved_cursor          = scene.cursor.location.copy()
        scene.cursor.location = h_loc
        bpy.ops.object.select_all(action='DESELECT')
        new_body.select_set(True)
        context.view_layer.objects.active = new_body
        bpy.ops.object.origin_set(type='ORIGIN_CURSOR')
        scene.cursor.location = saved_cursor

        item             = scene.mannequin_list.add()
        item.name        = new_head.name
        item.head_object = new_head
        item.body_object = new_body
        item.z_offset    = props.ref_z_offset
        item.sensitivity = 1.0

        props.active_index = len(scene.mannequin_list) - 1
        self.report({'INFO'}, f"Added {item.name}")
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
            for ob in (item.head_object, item.body_object):
                if ob:
                    bpy.data.objects.remove(ob, do_unlink=True)

        mlist.remove(idx)
        props.active_index = max(0, idx - 1)
        return {'FINISHED'}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        self.layout.prop(self, "delete_objects")


class MANNEQUIN_OT_reset_springs(bpy.types.Operator):
    """Clear all spring simulation state (useful after big timeline jumps)."""
    bl_idname = "mannequin.reset_springs"
    bl_label  = "Reset Springs"

    def execute(self, context):
        _spring_state.clear()
        mannequin_handler(context.scene)
        self.report({'INFO'}, "Spring states cleared.")
        return {'FINISHED'}


class MANNEQUIN_OT_refresh(bpy.types.Operator):
    """Force-recalculate all mannequin bodies at the current frame."""
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
            row.label(text=item.name, icon='ARMATURE_DATA')
            row.prop(item, "sensitivity", text="Tilt×", emboss=False)
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text="", icon='ARMATURE_DATA')


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
        box.label(text="Mannequins", icon='ARMATURE_DATA')
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
        row.operator("mannequin.refresh",      icon='FILE_REFRESH')
        row.operator("mannequin.reset_springs", icon='LOOP_BACK')

        layout.separator()

        # ── Hint ──
        box = layout.box()
        col = box.column(align=True)
        col.scale_y = 0.8
        col.label(text="Workflow:", icon='INFO')
        col.label(text="1. Assign Ref Head + Body (or Quick Default)")
        col.label(text="2. Set Z Offset, press + to add mannequins")
        col.label(text="3. Animate head objects — bodies update live")
        col.label(text="4. Tune Tilt + Spring sliders globally")
        col.label(text="5. Use Reset Springs after big timeline jumps")


# ─────────────────────────────────────────────────────────────
#  Persistent handler re-registration on file load
# ─────────────────────────────────────────────────────────────

@bpy.app.handlers.persistent
def _load_post_handler(dummy):
    _spring_state.clear()
    _register_handler()


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
    bpy.app.handlers.load_post.append(_load_post_handler)


def unregister():
    _unregister_handler()
    if _load_post_handler in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_load_post_handler)
    del bpy.types.Scene.mannequin_list
    del bpy.types.Scene.mannequin_props
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
