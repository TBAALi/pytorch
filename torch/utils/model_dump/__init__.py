#!/usr/bin/env python
"""
model_dump: a one-stop shop for TorchScript model inspection.

The goal of this tool is to provide a simple way to extract lots of
useful information from a TorchScript model and make it easy for humans
to consume.  It (mostly) replaces zipinfo, common uses of show_pickle,
and various ad-hoc analysis notebooks.

The tool extracts information from the model and serializes it as JSON.
That JSON can then be rendered by an HTML+JS page, either by
loading the JSON over HTTP or producing a fully self-contained page
with all of the code and data burned-in.
"""

# Maintainer notes follow.
"""
The implementation strategy has tension between 3 goals:
- Small file size.
- Fully self-contained.
- Easy, modern JS environment.
Using Preact and HTM achieves 1 and 2 with a decent result for 3.
However, the models I tested with result in ~1MB JSON output,
so even using something heavier like full React might be tolerable
if the build process can be worked out.

One principle I have followed that I think is very beneficial
is to keep the JSON data as close as possible to the model
and do most of the rendering logic on the client.
This makes for easier development (just refresh, usually),
allows for more laziness and dynamism, and lets us add more
views of the same data without bloating the HTML file.

Currently, this code doesn't actually load the model or even
depend on any part of PyTorch.  I don't know if that's an important
feature to maintain, but it's probably worth preserving the ability
to run at least basic analysis on models that cannot be loaded.

I think the easiest way to develop this code is to cd into model_dump and
run "python -m http.server", then load http://localhost:8000/skeleton.html
in the browser.  In another terminal, run
"python -m torch.utils.model_dump --style=json FILE > \
    torch/utils/model_dump/model_info.json"
every time you update the Python code.  When you update JS, just refresh.

Possible improvements:
    - Fix various TODO comments in this file and the JS.
    - Make the HTML much less janky, especially the auxiliary data panel.
    - Make the auxiliary data panel start small, expand when
      data is available, and have a button to clear/contract.
    - Clean up the JS.  There's a lot of copypasta because
      I don't really know how to use Preact.
    - Make the HTML render and work nicely inside a Jupyter notebook.
    - Add hyperlinking from data to code, and code to code.
    - Add hyperlinking from debug info to Diffusion.
    - Make small tensor contents available.
    - Do something nice for quantized models
      (they probably don't work at all right now). 
"""

import sys
import os
import io
import pathlib
import re
import argparse
import zipfile
import json
import pickle
import pprint
import importlib.resources
import urllib.parse

import torch.utils.show_pickle


DEFAULT_EXTRA_FILE_SIZE_LIMIT = 16 * 1024


def hierarchical_pickle(data):
    if isinstance(data, (bool, int, float, str, type(None))):
        return data
    if isinstance(data, list):
        return [ hierarchical_pickle(d) for d in data ]
    if isinstance(data, tuple):
        return {
            "__tuple_values__": hierarchical_pickle(list(data)),
        }
    if isinstance(data, dict):
        return {
            "__is_dict__": True,
            "keys": hierarchical_pickle(list(data.keys())),
            "values": hierarchical_pickle(list(data.values())),
        }
    if isinstance(data, torch.utils.show_pickle.FakeObject):
        typename = f"{data.module}.{data.name}"
        if data.module.startswith("__torch__."):
            assert data.args == ()
            return {
                "__module_type__": typename,
                "state": hierarchical_pickle(data.state),
            }
        if typename == "torch._utils._rebuild_tensor_v2":
            assert data.state is None
            storage, offset, size, stride, requires_grad, hooks = data.args
            assert isinstance(storage, torch.utils.show_pickle.FakeObject)
            assert storage.module == "pers"
            assert storage.name == "obj"
            assert storage.state is None
            assert isinstance(storage.args, tuple)
            assert len(storage.args) == 1
            assert isinstance(storage.args[0], tuple)
            assert len(storage.args[0]) == 5
            assert storage.args[0][0] == "storage"
            assert isinstance(storage.args[0][1], torch.utils.show_pickle.FakeClass)
            assert storage.args[0][1].module == "torch"
            assert storage.args[0][1].name.endswith("Storage")
            sa = storage.args[0]
            storage_info = [sa[1].name.replace("Storage", "")] + list(sa[2:])
            return {"__tensor_v2__": [storage_info, offset, size, stride, requires_grad]}
        if typename == "torch.jit._pickle.restore_type_tag":
            assert data.state is None
            obj, typ = data.args
            assert isinstance(typ, str)
            return hierarchical_pickle(obj)
        if re.fullmatch(r"torch\.jit\._pickle\.build_[a-z]+list", typename):
            assert data.state is None
            ls, = data.args
            assert isinstance(ls, list)
            return hierarchical_pickle(ls)
        raise Exception(f"Can't prepare fake object of type for JS: {typename}")
    raise Exception(f"Can't prepare data of type for JS: {type(data)}")


def get_model_info(
        path_or_file,
        title=None,
        extra_file_size_limit=DEFAULT_EXTRA_FILE_SIZE_LIMIT):
    """Get JSON-friendly informatino about a model.

    The result is suitable for being saved as model_info.json,
    or passed to burn_in_info.
    """

    if isinstance(path_or_file, os.PathLike):
        default_title = os.fspath(path_or_file)
        file_size = path_or_file.stat().st_size
    elif isinstance(path_or_file, str):
        default_title = path_or_file
        file_size = pathlib.Path(path_or_file).stat().st_size
    else:
        default_title = "buffer"
        path_or_file.seek(0, io.SEEK_END)
        file_size = path_or_file.tell()
        path_or_file.seek(0)

    title = title or default_title

    with zipfile.ZipFile(path_or_file) as zf:
        path_prefix = None
        zip_files = []
        for zi in zf.infolist():
            prefix = re.sub("/.*", "", zi.filename)
            if path_prefix is None:
                path_prefix = prefix
            elif prefix != path_prefix:
                raise Exception(f"Mismatched prefixes: {path_prefix} != {prefix}")
            zip_files.append(dict(
                    filename=zi.filename,
                    compression=zi.compress_type,
                    compressed_size=zi.compress_size,
                    file_size=zi.file_size,
                    ))

        assert path_prefix != None
        version = zf.read(path_prefix + "/version").decode("utf-8").strip()

        with zf.open(path_prefix + "/data.pkl") as handle:
            raw_model_data = torch.utils.show_pickle.DumpUnpickler.dump(handle, out_stream=io.StringIO())
            model_data = hierarchical_pickle(raw_model_data)

        # Intern strings that are likely to be re-used.
        # Pickle automatically detects shared structure,
        # so re-used strings are stored efficiently.
        # However, JSON has no way of representing this,
        # so we have to do it manually.
        interned_strings = {}

        code_files = {}
        def ist(s):
            if s not in interned_strings:
                interned_strings[s] = len(interned_strings)
            return interned_strings[s]

        for zi in zf.infolist():
            if not zi.filename.endswith(".py"):
                continue
            with zf.open(zi) as handle:
                raw_code = handle.read()
            with zf.open(zi.filename + ".debug_pkl") as handle:
                raw_debug = handle.read()
            debug_info = pickle.loads(raw_debug)
            code_parts = []
            for di, di_next in zip(debug_info, debug_info[1:]):
                start, source_range = di
                end = di_next[0]
                assert end > start
                source, s_start, s_end = source_range
                s_text, s_file, s_line = source
                # TODO: Handle this case better.  TorchScript ranges are in bytes,
                # but JS doesn't really handle byte strings.  Bail out if
                # bytes and chars are not equivalent for this string.
                if len(s_text) != len(s_text.encode("utf-8")):
                    s_text = ""
                    s_start = 0
                    s_end = 0
                text = raw_code[start:end]
                code_parts.append([text.decode("utf-8"), ist(s_file), s_line, ist(s_text), s_start, s_end])
            # TODO: Handle cases where debug info is missing or doesn't cover the full source.
            code_files[zi.filename] = code_parts

        extra_files_json_pattern = re.compile(re.escape(path_prefix) + "/extra/.*\\.json")
        extra_files_jsons = {}
        for zi in zf.infolist():
            if not extra_files_json_pattern.fullmatch(zi.filename):
                continue
            if zi.file_size > extra_file_size_limit:
                continue
            with zf.open(zi) as handle:
                # TODO: handle errors here and just ignore the file?
                json_content = json.load(handle)
            extra_files_jsons[zi.filename] = json_content

        always_render_pickles = {
            "bytecode.pkl",
        }
        extra_pickles = {}
        for zi in zf.infolist():
            if not zi.filename.endswith(".pkl"):
                continue
            with zf.open(zi) as handle:
                # TODO: handle errors here and just ignore the file?
                # NOTE: For a lot of these files (like bytecode),
                # we could get away with just unpickling, but this should be safer.
                obj = torch.utils.show_pickle.DumpUnpickler.dump(handle, out_stream=io.StringIO())
            buf = io.StringIO()
            pprint.pprint(obj, buf)
            contents = buf.getvalue()
            # Checked the rendered length instead of the file size
            # because pickles with shared structure can explode in size during rendering.
            if os.path.basename(zi.filename) not in always_render_pickles and \
                    len(contents) > extra_file_size_limit:
                continue
            extra_pickles[zi.filename] = contents


    return {"model": dict(
        title=title,
        file_size=file_size,
        version=version,
        zip_files=zip_files,
        interned_strings=list(interned_strings),
        code_files=code_files,
        model_data=model_data,
        extra_files_jsons=extra_files_jsons,
        extra_pickles=extra_pickles,
        )}


def get_inline_skeleton():
    """Get a fully-inlined skeleton of the frontend.

    The returned HTML page has no external network dependencies for code.
    It can load model_info.json over HTTP, or be passed to burn_in_info.
    """

    skeleton = importlib.resources.read_text(__package__, "skeleton.html")
    js_code = importlib.resources.read_text(__package__, "code.js")
    for js_module in ["preact", "htm"]:
        js_lib = importlib.resources.read_binary(__package__, f"{js_module}.mjs")
        js_url = "data:application/javascript," + urllib.parse.quote(js_lib)
        js_code = js_code.replace(f"https://unpkg.com/{js_module}?module", js_url)
    skeleton = skeleton.replace(' src="./code.js">', ">\n" + js_code)
    return skeleton


def burn_in_info(skeleton, info):
    """Burn model info into the HTML skeleton.

    The result will render the hard-coded model info and
    have no external network dependencies for code or data.
    """

    return skeleton.replace(
            "BURNED_IN_MODEL_INFO = null",
            "BURNED_IN_MODEL_INFO = " + json.dumps(info).replace("/", "\/"))


def main(argv, stdout=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--style", choices=["json", "html"])
    parser.add_argument("--title")
    parser.add_argument("model")
    args = parser.parse_args(argv[1:])

    info = get_model_info(args.model, title=args.title)

    output = stdout or sys.stdout

    if args.style == "json":
        output.write(json.dumps(info) + "\n")
    elif args.style == "html":
        skeleton = get_inline_skeleton()
        page = burn_in_info(skeleton, info)
        output.write(page)
    else:
        raise Exception("Invalid style")