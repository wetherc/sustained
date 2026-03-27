from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        # Add .pyi and py.typed files to the shared source
        build_data["force_include"]["src/sustained"] = "sustained"
