bl_info = {
    "name": "Mannequin Follow Lag",
    "author": "Custom",
    "version": (2, 1, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > Mannequin",
    "description": (
        "Body copies head XY position, rotates opposite to movement direction. "
        "Live via frame-change handler. Supports multiple mannequins."
    ),
    "category": "Animation",
}

import bpy
import mathutils
import math


# ─────────────────────────────────────────────────────────────
#  Core: sample head velocity and apply body transform
# ─────────────────────────────────────────────────────────────

# Guard against re-entrant frame_set calls inside the handler
_handler_running = False


def _sample_pos(head_obj, scene, frame):
    """Sample head world XY position at a specific frame (clamped to range)."""
    f = max(scene.frame_start, min(scene.frame_end, frame))
    scene.frame_set(f)
    p = head_obj.matrix_world.to_translation().copy()
    return mathutils.Vector((p.x, p.y, 0.0))


def _get_head_motion(head_obj, scene, delay_frames):
    """
    Return (velocity, acceleration) as XY vectors, evaluated at
    (current_frame - delay_frames) using central differences.

    velocity     = direction + speed of travel at the delayed frame
    acceleration = change in velocity (negative dot velocity = decelerating)
    """
    cur = scene.frame_current
    t   = cur - delay_frames          # evaluate motion at this delayed frame

    p_prev2 = _sample_pos(head_obj, scene, t - 2)
    p_prev  = _sample_pos(head_obj, scene, t - 1)
    p_cur   = _sample_pos(head_obj, scene, t)
    p_next  = _sample_pos(head_obj, scene, t + 1)
    p_next2 = _sample_pos(head_obj, scene, t + 2)

    # restore to actual current frame
    scene.frame_set(cur)

    # central-difference velocity at t-1, t, t+1
    vel_prev = (p_cur   - p_prev2) * 0.5
    vel_cur  = (p_next  - p_prev)  * 0.5
    vel_next = (p_next2 - p_cur)   * 0.5

    # central-difference acceleration at t
    accel = (vel_next - vel_prev) * 0.5

    return vel_cur, accel


def _update_mannequin(item, scene, global_scale, max_tilt_rad, delay_frames, counter_scale):
    """Apply position + counter-rotation to one mannequin body."""
    head_obj = item.head_object
    body_obj = item.body_object
    if head_obj is None or body_obj is None:
        return

    # ── Position: XY matches head, Z = head - offset ──
    h_world = head_obj.matrix_world.to_translation()
    body_obj.location = mathutils.Vector((
        h_world.x,
        h_world.y,
        h_world.z - item.z_offset,
    ))

    # ── Sample motion at (current - delay) ──
    vel, accel = _get_head_motion(head_obj, scene, delay_frames)
    speed = vel.length

    body_obj.rotation_mode = 'QUATERNION'
    z_up = mathutils.Vector((0.0, 0.0, 1.0))

    # ── Base tilt: lean opposite to velocity direction ──
    if speed > 0.00001:
        vel_dir   = vel.normalized()
        tilt_axis = vel_dir.cross(z_up)
        base_angle = min(speed * item.sensitivity * global_scale, max_tilt_rad)
    else:
        vel_dir    = mathutils.Vector((0.0, 0.0, 0.0))
        tilt_axis  = mathutils.Vector((1.0, 0.0, 0.0))
        base_angle = 0.0

    # ── Counter-movement: deceleration overshoot ──
    # When accel opposes vel (dot < 0) the body swings forward past upright.
    accel_mag     = accel.length
    counter_angle = 0.0
    counter_axis  = tilt_axis

    if accel_mag > 0.00001 and counter_scale > 0.0:
        accel_dir = accel.normalized()
        dot = vel_dir.dot(accel_dir) if speed > 0.00001 else 0.0
        if dot < -0.1:
            counter_axis  = accel_dir.cross(z_up)
            counter_angle = min(
                accel_mag * item.sensitivity * counter_scale,
                max_tilt_rad,
            )

    # ── Combine: base lean + counter overshoot ──
    q_base    = mathutils.Quaternion(tilt_axis,    -base_angle)
    q_counter = mathutils.Quaternion(counter_axis,  counter_angle)
    body_obj.rotation_quaternion = q_base @ q_counter


def mannequin_handler(scene):
    """Frame-change handler: updates all registered mannequin bodies."""
    global _handler_running
    if _handler_running:
        return
    _handler_running = True

    try:
        mlist = scene.mannequin_list
        if not mlist:
            return

        props         = scene.mannequin_props
        global_scale  = props.counter_rotation_scale
        max_tilt_rad  = math.radians(props.max_tilt_degrees)
        delay_frames  = props.delay_frames
        counter_scale = props.counter_movement_scale

        for item in mlist:
            _update_mannequin(item, scene, global_scale, max_tilt_rad, delay_frames, counter_scale)

    finally:
        _handler_running = False


def _register_handler():
    if mannequin_handler not in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.append(mannequin_handler)


def _unregister_handler():
    if mannequin_handler in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.remove(mannequin_handler)


# ─────────────────────────────────────────────────────────────
#  Property Groups
# ─────────────────────────────────────────────────────────────

def _on_global_change(self, context):
    mannequin_handler(context.scene)


class MannequinItem(bpy.types.PropertyGroup):
    """Represents one head+body pair in the scene."""
    name: bpy.props.StringProperty(name="Name", default="Mannequin")
    head_object: bpy.props.PointerProperty(
        name="Head", type=bpy.types.Object,
        description="The animated controller object",
    )
    body_object: bpy.props.PointerProperty(
        name="Body", type=bpy.types.Object,
        description="The object that counter-rotates",
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
    """Scene-level settings: reference objects + global tuning."""
    ref_head: bpy.props.PointerProperty(
        name="Reference Head", type=bpy.types.Object,
        description="Mesh to duplicate for each new mannequin head",
    )
    ref_body: bpy.props.PointerProperty(
        name="Reference Body", type=bpy.types.Object,
        description="Mesh to duplicate for each new mannequin body",
    )
    ref_z_offset: bpy.props.FloatProperty(
        name="Z Offset",
        description="Vertical distance between head center and body center",
        default=0.65, min=0.0, max=10.0, precision=3,
    )
    counter_rotation_scale: bpy.props.FloatProperty(
        name="Global Tilt Scale",
        description="Global multiplier for ALL mannequin tilts",
        default=3.0, min=0.0, max=20.0, step=10, precision=2,
        update=_on_global_change,
    )
    max_tilt_degrees: bpy.props.FloatProperty(
        name="Max Tilt °",
        description="Maximum tilt angle (degrees) for all mannequins",
        default=35.0, min=0.0, max=90.0, step=100,
        update=_on_global_change,
    )
    delay_frames: bpy.props.IntProperty(
        name="Delay Frames",
        description="How many frames behind the body reacts to head movement",
        default=0, min=0, max=30,
        update=_on_global_change,
    )
    counter_movement_scale: bpy.props.FloatProperty(
        name="Counter Movement Scale",
        description="How strongly the body overshoots on deceleration",
        default=2.0, min=0.0, max=20.0, step=10, precision=2,
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
        bev           = body.modifiers.new("Bevel", 'BEVEL')
        bev.width     = 0.08
        bev.segments  = 4

        props.ref_head     = head
        props.ref_body     = body
        props.ref_z_offset = 0.65

        self.report({'INFO'}, "Reference objects created. Assign them above, then click +.")
        return {'FINISHED'}


class MANNEQUIN_OT_create(bpy.types.Operator):
    """Duplicate the reference objects and register as a new mannequin."""
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

        # ── Duplicate head ──
        bpy.ops.object.select_all(action='DESELECT')
        props.ref_head.select_set(True)
        context.view_layer.objects.active = props.ref_head
        bpy.ops.object.duplicate(linked=False)
        new_head      = context.active_object
        new_head.name = f"Mannequin_{idx:02d}_Head"
        # Place slightly offset so duplicates don't stack
        new_head.location.x += idx * 1.0

        # ── Duplicate body ──
        bpy.ops.object.select_all(action='DESELECT')
        props.ref_body.select_set(True)
        context.view_layer.objects.active = props.ref_body
        bpy.ops.object.duplicate(linked=False)
        new_body      = context.active_object
        new_body.name = f"Mannequin_{idx:02d}_Body"

        # Position body relative to head
        h_loc = new_head.location.copy()
        new_body.location = mathutils.Vector((
            h_loc.x,
            h_loc.y,
            h_loc.z - props.ref_z_offset,
        ))

        # ── Set body origin to head center (rotation anchor) ──
        saved_cursor          = scene.cursor.location.copy()
        scene.cursor.location = h_loc
        bpy.ops.object.select_all(action='DESELECT')
        new_body.select_set(True)
        context.view_layer.objects.active = new_body
        bpy.ops.object.origin_set(type='ORIGIN_CURSOR')
        scene.cursor.location = saved_cursor

        # ── Register ──
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
        name="Also delete objects from scene",
        default=False,
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
        box.prop(props, "ref_head",      icon='MESH_UVSPHERE')
        box.prop(props, "ref_body",      icon='MESH_CYLINDER')
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

        # Per-item detail
        if 0 <= props.active_index < len(mlist):
            item = mlist[props.active_index]
            sub  = box.box()
            sub.label(text=item.name, icon='SETTINGS')
            sub.prop(item, "head_object")
            sub.prop(item, "body_object")
            sub.prop(item, "z_offset",    slider=False)
            sub.prop(item, "sensitivity", slider=True)

        layout.separator()

        # ── Global tuning ──
        box = layout.box()
        box.label(text="Global Tuning (all mannequins)", icon='PREFERENCES')
        box.prop(props, "counter_rotation_scale",  slider=True)
        box.prop(props, "max_tilt_degrees",         slider=True)
        box.prop(props, "delay_frames",             slider=True)
        box.prop(props, "counter_movement_scale",   slider=True)
        box.operator("mannequin.refresh", icon='FILE_REFRESH')

        layout.separator()

        # ── Hint ──
        box = layout.box()
        col = box.column(align=True)
        col.scale_y = 0.8
        col.label(text="Workflow:", icon='INFO')
        col.label(text="1. Assign Ref Head + Body (or Quick Default)")
        col.label(text="2. Set Z Offset")
        col.label(text="3. Press + to create new mannequin pairs")
        col.label(text="4. Animate head objects — bodies update live")
        col.label(text="5. Tune Global Tilt Scale / Max Tilt °")


# ─────────────────────────────────────────────────────────────
#  Persistent re-registration on file load
# ─────────────────────────────────────────────────────────────

@bpy.app.handlers.persistent
def _load_post_handler(dummy):
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
