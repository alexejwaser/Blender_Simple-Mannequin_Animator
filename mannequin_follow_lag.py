bl_info = {
    "name": "Mannequin Follow Lag",
    "author": "Custom",
    "version": (1, 0, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > Mannequin",
    "description": "Animate a head controller; body follows with lag and easing",
    "category": "Animation",
}

import bpy
import mathutils


# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────

def lerp_vec(a, b, t):
    return a.lerp(b, t)

def lerp_quat(a, b, t):
    return a.slerp(b, t)

def ease_in_out(t):
    """Smooth-step easing curve (0-1)."""
    return t * t * (3.0 - 2.0 * t)


# ─────────────────────────────────────────────
#  Bake operator
# ─────────────────────────────────────────────

class MANNEQUIN_OT_bake_follow(bpy.types.Operator):
    """Bake the body object so it follows the head with lag and easing"""
    bl_idname  = "mannequin.bake_follow"
    bl_label   = "Bake Body Follow"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene  = context.scene
        props  = scene.mannequin_props

        head_obj = props.head_object
        body_obj = props.body_object

        if head_obj is None or body_obj is None:
            self.report({'ERROR'}, "Please assign both Head and Body objects.")
            return {'CANCELLED'}

        frame_start = scene.frame_start
        frame_end   = scene.frame_end

        # lag in frames – how far behind the body is
        lag_frames = props.lag_frames
        # smoothing factor for per-frame lerp (lower = more sluggish)
        smooth     = props.smoothness   # 0.01 – 1.0

        # ── collect head transforms at every frame ──
        head_locs  = {}
        head_rots  = {}

        orig_frame = scene.frame_current
        for f in range(frame_start, frame_end + 1):
            scene.frame_set(f)
            head_locs[f] = head_obj.matrix_world.to_translation().copy()
            head_rots[f] = head_obj.matrix_world.to_quaternion().copy()

        # ── initialise body state ──
        # seed the body at head position at start frame
        body_loc = head_locs[frame_start].copy()
        body_rot = head_rots[frame_start].copy()

        # ── clear existing body keyframes on loc/rot ──
        body_obj.animation_data_create()
        action = bpy.data.actions.new(name="BodyFollowBaked")
        body_obj.animation_data.action = action

        # ── per-frame spring simulation ──
        for f in range(frame_start, frame_end + 1):
            # target frame for head is lag_frames in the past (clamp to start)
            src_f = max(frame_start, f - lag_frames)

            target_loc = head_locs[src_f]
            target_rot = head_rots[src_f]

            # smooth lerp toward target
            t_ease = ease_in_out(min(smooth, 1.0))
            body_loc = lerp_vec(body_loc, target_loc, t_ease)
            body_rot = lerp_quat(body_rot, target_rot, t_ease)
            body_rot.normalize()

            # apply offset so body sits below head
            offset = mathutils.Vector((0.0, 0.0, -props.body_offset))
            rotated_offset = body_rot @ offset
            final_loc = body_loc + rotated_offset

            # write keyframe
            body_obj.location = final_loc
            body_obj.rotation_mode = 'QUATERNION'
            body_obj.rotation_quaternion = body_rot
            body_obj.keyframe_insert(data_path="location",            frame=f)
            body_obj.keyframe_insert(data_path="rotation_quaternion", frame=f)

        # restore frame
        scene.frame_set(orig_frame)

        self.report({'INFO'}, f"Baked {frame_end - frame_start + 1} frames.")
        return {'FINISHED'}


class MANNEQUIN_OT_clear_bake(bpy.types.Operator):
    """Remove the baked action from the body object"""
    bl_idname  = "mannequin.clear_bake"
    bl_label   = "Clear Baked Keys"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        body_obj = context.scene.mannequin_props.body_object
        if body_obj and body_obj.animation_data and body_obj.animation_data.action:
            action = body_obj.animation_data.action
            body_obj.animation_data.action = None
            bpy.data.actions.remove(action)
            self.report({'INFO'}, "Cleared baked animation.")
        else:
            self.report({'WARNING'}, "No baked action found on body.")
        return {'FINISHED'}


class MANNEQUIN_OT_setup_scene(bpy.types.Operator):
    """Create a simple mannequin (head sphere + body capsule) in the scene"""
    bl_idname  = "mannequin.setup_scene"
    bl_label   = "Create Mannequin"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        props = scene.mannequin_props

        # ── Head ──
        bpy.ops.mesh.primitive_uv_sphere_add(radius=0.2, location=(0, 0, 1.7))
        head = context.active_object
        head.name = "MannequinHead"

        # ── Body ──
        bpy.ops.mesh.primitive_cylinder_add(
            radius=0.18, depth=0.7,
            location=(0, 0, 1.05)
        )
        body = context.active_object
        body.name = "MannequinBody"

        # round the body ends with bevel (visual only)
        bevel = body.modifiers.new("Bevel", 'BEVEL')
        bevel.width = 0.08
        bevel.segments = 4

        # auto-assign
        props.head_object = head
        props.body_object = body

        self.report({'INFO'}, "Mannequin created! Animate the Head, then click Bake.")
        return {'FINISHED'}


# ─────────────────────────────────────────────
#  Properties
# ─────────────────────────────────────────────

class MannequinProperties(bpy.types.PropertyGroup):
    head_object: bpy.props.PointerProperty(
        name="Head",
        type=bpy.types.Object,
        description="The object you will animate (the controller)",
    )
    body_object: bpy.props.PointerProperty(
        name="Body",
        type=bpy.types.Object,
        description="The object that follows with lag",
    )
    lag_frames: bpy.props.IntProperty(
        name="Lag Frames",
        description="How many frames the body lags behind the head",
        default=6,
        min=0,
        max=60,
    )
    smoothness: bpy.props.FloatProperty(
        name="Smoothness",
        description="Per-frame blend speed toward target (lower = sluggish)",
        default=0.18,
        min=0.01,
        max=1.0,
        step=1,
        precision=2,
    )
    body_offset: bpy.props.FloatProperty(
        name="Body Z Offset",
        description="How far below the head the body center sits (world units)",
        default=0.65,
        min=0.0,
        max=5.0,
    )


# ─────────────────────────────────────────────
#  Panel
# ─────────────────────────────────────────────

class MANNEQUIN_PT_panel(bpy.types.Panel):
    bl_label       = "Mannequin Follow Lag"
    bl_idname      = "MANNEQUIN_PT_panel"
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category    = "Mannequin"

    def draw(self, context):
        layout = self.layout
        props  = context.scene.mannequin_props
        scene  = context.scene

        # ── Quick Setup ──
        box = layout.box()
        box.label(text="Quick Setup", icon='ARMATURE_DATA')
        box.operator("mannequin.setup_scene", icon='ADD')

        layout.separator()

        # ── Objects ──
        box = layout.box()
        box.label(text="Objects", icon='OBJECT_DATA')
        box.prop(props, "head_object", icon='MESH_UVSPHERE')
        box.prop(props, "body_object", icon='MESH_CYLINDER')

        layout.separator()

        # ── Settings ──
        box = layout.box()
        box.label(text="Follow Settings", icon='SETTINGS')
        box.prop(props, "lag_frames",  slider=True)
        box.prop(props, "smoothness",  slider=True)
        box.prop(props, "body_offset", slider=True)

        layout.separator()

        # ── Frame range info ──
        row = layout.row()
        row.label(text=f"Frame Range: {scene.frame_start} – {scene.frame_end}")

        layout.separator()

        # ── Actions ──
        col = layout.column(align=True)
        col.scale_y = 1.4
        col.operator("mannequin.bake_follow", icon='RENDER_ANIMATION')
        col.operator("mannequin.clear_bake",  icon='TRASH')

        layout.separator()

        # ── Usage hint ──
        box = layout.box()
        col = box.column(align=True)
        col.scale_y = 0.85
        col.label(text="Workflow:", icon='INFO')
        col.label(text="1. Click 'Create Mannequin' (or assign objects)")
        col.label(text="2. Keyframe the HEAD object only")
        col.label(text="3. Adjust Lag & Smoothness")
        col.label(text="4. Click 'Bake Body Follow'")
        col.label(text="5. Re-bake anytime after changes")


# ─────────────────────────────────────────────
#  Register
# ─────────────────────────────────────────────

classes = (
    MannequinProperties,
    MANNEQUIN_OT_setup_scene,
    MANNEQUIN_OT_bake_follow,
    MANNEQUIN_OT_clear_bake,
    MANNEQUIN_PT_panel,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.mannequin_props = bpy.props.PointerProperty(type=MannequinProperties)

def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.mannequin_props

if __name__ == "__main__":
    register()
