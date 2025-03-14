from contextlib import contextmanager
import os
from pathlib import Path
import re
import tempfile
from xml.dom.minidom import parseString

import rospkg

from . import xacro, packages


def _xacro_include(path):
    return f"""
    <xacro:include filename="{path}" />
    """


def _xacro_header(name):
    return f"""<?xml version="1.0" encoding="utf-8"?>
    <robot name="{name}" xmlns:xacro="http://www.ros.org/wiki/xacro">
    """.strip()


def _resolve_package_protocol(text):
    """Resolve file names for meshes specified using `package://`"""
    pkg_names = re.findall(r"package://([\w-]+)", text)
    for pkg in pkg_names:
        abspath = Path(packages.get_path(pkg)).absolute().as_posix()
        text = re.sub(f"package://{pkg}", f"file://{abspath}", text)
    return text


def _compile_xacro_file(
    text,
    subargs=None,
    max_runs=10,
    resolve_packages=True,
    remove_protocols=False,
):
    """Compile xacro string until a fixed point is reached.

    Parameters
    ----------
    text : str
        The xacro string.
    subargs : dict or None
        Optional dictionary of substitution arguments to pass into xacro.
    max_runs : int
        Maximum number of compilation runs.
    resolve_packages : bool
        If ``True``, resolve package protocol URIs in the compiled URDF.
        Otherwise, they are left unchanged.
    remove_protocols : bool
        If ``True``, remove file:// protocols in the compiled URDF.
        Otherwise, they are left unchanged.

    Raises
    ------
    ValueError
        If ``max_runs`` compilations are exceeded.

    Returns
    -------
    : xml.dom.minidom.Document
        The URDF XML document.
    """
    if subargs is None:
        subargs = {}

    doc = xacro.parse(text)
    s1 = doc.toxml()

    # keep compiling until a fixed point is reached (i.e., the document doesn't
    # change anymore)
    run = 1
    while run < max_runs:
        xacro.process_doc(doc, mappings=subargs)  # modifies doc in place
        s2 = doc.toxml()
        if s1 == s2:
            break
        s1 = s2
        run += 1

    if run >= max_runs:
        raise ValueError("URDF file did not converge.")

    if resolve_packages or remove_protocols:
        # TODO could perhaps be more clever than converting from and back to
        # XML
        text = doc.toxml()
        if resolve_packages:
            text = _resolve_package_protocol(text)
        if remove_protocols:
            text = re.sub(f"file://", "", text)

        doc = xacro.parse(text)

    return doc


class XacroDoc:
    """Convenience class to build URDF strings and files out of xacro components.

    Parameters
    ----------
    text : str
        The xacro text to compile into a URDF document.
    subargs : dict or None
        Optional dict of substitution arguments; i.e., the values of
        ``<xacro:arg ...>`` directives. Equivalent to writing ``value:=foo`` on
        the command line.
    max_runs : int
        The text is repeated compiled until a fixed point is reached; i.e.,
        compilation of the text just returns the same text. ``max_runs`` is the
        maximum number of times compilation will be performed.
    resolve_packages : bool
        If ``True``, resolve package protocol URIs in the compiled URDF.
        Otherwise, they are left unchanged.
    remove_protocols : bool
        If ``True``, remove file:// protocols in the compiled URDF.
        Otherwise, they are left unchanged.

    Attributes
    ----------
    doc :
        The underlying XML document.
    """

    def __init__(
        self,
        text,
        subargs=None,
        max_runs=10,
        resolve_packages=True,
        remove_protocols=False,
    ):
        self.doc = _compile_xacro_file(
            text=text,
            subargs=subargs,
            max_runs=max_runs,
            resolve_packages=resolve_packages,
            remove_protocols=remove_protocols,
        )

    @classmethod
    def from_file(cls, path, walk_up=True, **kwargs):
        """Load the URDF document from a xacro file.

        Parameters
        ----------
        path : str or Path
            The path to the xacro file.
        walk_up : bool
            If ``True``, look for packages by walking up the directory tree
            from ``path``.
        """
        if walk_up:
            packages.walk_up_from(path)

        with open(path) as f:
            text = f.read()
        return cls(text, **kwargs)

    @classmethod
    def from_package_file(cls, package_name, relative_path, **kwargs):
        """Load the URDF document from a xacro file in a ROS package.

        Parameters
        ----------
        package_name : str
            The name of the ROS package.
        relative_path : str or Path
            The path of the xacro file relative to the ROS package.
        """
        path = packages.get_file_path(package_name, relative_path)
        return cls.from_file(path, **kwargs)

    @classmethod
    def from_includes(cls, includes, name="robot", **kwargs):
        """Build the URDF document from xacro includes.

        Parameters
        ----------
        includes : list
            A list of strings representing files to include in a new, otherwise
            empty, xacro file. Any string that can be used in a xacro include
            directive is valid, so look-ups like ``$(find package)`` are valid.
        name : str
            The name of the top-level robot tag.
        """
        s = _xacro_header(name)
        for incl in includes:
            s += _xacro_include(incl)
        s += "</robot>"
        return cls(s, **kwargs)

    def add_mujoco_extension(self):
        """Modify the document to conversion to Mujoco MJCF XML format.

        Currently this sets the strippath compiler attribute to false, so that
        full paths can be used for meshes.
        """
        mujoco_nodes = self.doc.getElementsByTagName("mujoco")
        if len(mujoco_nodes) > 1:
            raise ValueError("Multiple <mujoco> elements found.")
        if len(mujoco_nodes) == 0:
            mujoco_node = self.doc.createElement("mujoco")
            self.doc.documentElement.appendChild(mujoco_node)
        else:
            mujoco_node = mujoco_nodes[0]

        compiler_nodes = mujoco_node.getElementsByTagName("compiler")
        if len(compiler_nodes) > 1:
            raise ValueError("Multiple <compiler> elements found.")
        if len(compiler_nodes) == 0:
            compiler_node = self.doc.createElement("compiler")
            mujoco_node.appendChild(compiler_node)
        else:
            compiler_node = compiler_nodes[0]

        compiler_node.setAttribute("strippath", "false")

    def to_mjcf_file(self, path):
        """Convert and write to a Mujoco MJCF XML file.

        This requires mujoco module to be installed and available for import.

        Parameters
        ----------
        path : str or Path
            The path to the MJCF XML file to be written.
        """
        import mujoco

        path = Path(path).as_posix()

        urdf_str = self.to_urdf_string()
        model = mujoco.MjModel.from_xml_string(urdf_str)
        mujoco.mj_saveLastXML(path, model)

    def to_urdf_file(self, path, compare_existing=True, verbose=False):
        """Write the URDF to a file.

        Parameters
        ----------
        path : str or Path
            The path to the URDF file to be written.
        compare_existing : bool
            If ``True``, then if the file at ``path`` already exists, read it
            and only write back to it if the parsed URDF is different than the
            file content. This avoids some race conditions if the file is being
            compiled by multiple processes concurrently.
        verbose : bool
            Set to ``True`` to print information about xacro compilation,
            ``False`` otherwise.
        """
        s = self.to_urdf_string(pretty=True)

        # if the full path already exists, we can check if the contents are the
        # same to avoid writing it if it hasn't changed. This avoids some race
        # conditions if the file is being compiled by multiple processes
        # concurrently.
        if Path(path).exists() and compare_existing:
            with open(path) as f:
                s0 = f.read()
            if s0 == s:
                if verbose:
                    print("URDF files are the same - not writing.")
                return
            elif verbose:
                print("URDF files are not the same - writing.")

        with open(path, "w") as f:
            f.write(s)

    def to_temp_urdf_file(self, verbose=False):
        """Write the URDF to a temporary file.

        Clean-up is left to the user. If you want the tempfile to be
        automatically deleted, use the context manager ``temp_urdf_file_path``.

        Parameters
        ----------
        verbose : bool
            Set to ``True`` to print information about xacro compilation,
            ``False`` otherwise.

        Returns
        -------
        : str
            The path to the temporary URDF file.
        """
        _, path = tempfile.mkstemp(suffix=".urdf")
        self.to_urdf_file(path, compare_existing=False, verbose=verbose)
        return path

    @contextmanager
    def temp_urdf_file_path(self, verbose=False):
        """Get the context of a temporary URDF file path.

        This is useful for temporarily writing the URDF to a file for
        consumption by other programs.

        Parameters
        ----------
        verbose : bool
            Set to ``True`` to print information about xacro compilation,
            ``False`` otherwise.

        Examples
        --------
        Use as a context manager:

        .. code-block:: python

           with doc.temp_urdf_file_path() as path:
               load_file_from_path(path)
        """
        _, path = tempfile.mkstemp(suffix=".urdf")
        try:
            self.to_urdf_file(path, compare_existing=False, verbose=verbose)
            yield path
        finally:
            os.remove(path)

    def to_urdf_string(self, pretty=True):
        """Get the URDF as a string.

        Parameters
        ----------
        pretty : bool
            True to format the string for human readability, False otherwise.

        Returns
        -------
        : str
            The URDF represented as a string.
        """
        if pretty:
            return self.doc.toprettyxml(indent="  ")
        return self.doc.toxml()
