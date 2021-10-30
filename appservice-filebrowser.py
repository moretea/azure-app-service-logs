#!/usr/bin/env nix-shell
#!nix-shell -p python3Packages.asciimatics -p python3Packages.requests -p python3Packages.click -i python3

# A filebrowser tool for the AppService logs in Azure.
import sys
import click
import typing
import collections
import datetime

from base64 import b64encode
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import requests

## Domain objects
class Path(collections.UserString):
    def parse(path):
      if path.startswith("/home/"):
        return Path(path)
      else:
        raise Exception("Paths must be prefixed with /home/")
    def is_dir(self):
        return self.data[-1] == '/'

@dataclass
class Node:
    path: str
    name: str
    href: str
    crtime: str
    mtime: str
    mime: str
    size: int

    def is_dir(self):
        return self.mime == "inode/directory"

@dataclass
class Directory:
    path: str
    nodes: typing.List[Node]


# Plumbing

class VfsClient:
  def __init__(self, user, password, publish_url):
      self.user = user
      self.password = password
      self.publish_url = publish_url
  def _request(self, path, method="GET", **kwargs):
      headers = kwargs.get("headers", {})
      headers["Authorization"] = "Basic " +  b64encode((self.user + ':' + self.password).encode('ascii')).decode('ascii')

      kwargs["headers"] = headers
      return requests.request(url=self.publish_url + path, method=method, **kwargs)

  def _get_path(self, path):
    url_parts = path[5:] # Converts a /home/a/b/c path to url parts by stripping /home/
    return self._request("/api/vfs/{}".format(url_parts))

  def list_dir(self, path):
    r = self._get_path(path)
    if "application/json" in r.headers["Content-Type"]:
      # use splat to assign values to node, not most friendly for API stability, but easy as fuck though ;)
      return Directory(path=path, nodes=[Node(**node) for node in r.json()])
    else:
      raise click.UsageError("This is not a directory. Tip: use PROG get {} if this is a file".format(path))

  def get_file(self, path):
    r = self._get_path(path)
    return r.content

## TUI
from asciimatics.widgets import Frame, ListBox, Layout, Divider, Text, \
    Button, TextBox, Widget, Label, TextBox
from asciimatics.scene import Scene
from asciimatics.screen import Screen
from asciimatics.exceptions import ResizeScreenError, NextScene, StopApplication

class BrowserView(Frame):
    def __init__(self, screen, controller):
        super(BrowserView, self).__init__(screen,
                                       screen.height,
                                       screen.width,
                                       on_load=self._load_listview,
                                       hover_focus=True,
                                       can_scroll=False,
                                       title="Browser")
        self.controller=controller

        self._list_view = ListBox(
            Widget.FILL_FRAME,
            name="nodes",
            options=[],
            add_scroll_bar=True,
            on_change=self._change_selected,
            on_select=self._item_action)
        layout = Layout([1], fill_frame=True)
        self.add_layout(layout)
        layout.add_widget(self._list_view,0)
        layout.add_widget(Divider())

        layout_details = Layout([1])
        self.add_layout(layout_details)
        self._label_details_time = Label("")
        self._label_details_mime = Label("")
        layout_details.add_widget(self._label_details_time)
        layout_details.add_widget(self._label_details_mime)

        layout2 = Layout([1,1,1,1])
        self.add_layout(layout2)
        layout2.add_widget(Button("Refresh", self._refresh))
        layout2.add_widget(Button("Quit", self._quit), 3)
        self.fix()


    def _load_listview(self):
        d = self.controller.get_current_dir()
        def node2listview_option(node):
            if node.is_dir():
                txt = "{}/".format(node.name)
            else:
                txt = "{name} {size}".format(name=node.name, size=byte_size_to_human_size(node.size))
            return (txt, node)
        self._list_view.options = list(map(node2listview_option, d.nodes))

        if not self.controller.in_root_dir():
            self._list_view.options.insert(0, ("..", self.controller.GOTO_PARENT))

        self.title = str(d.path)

    def _change_selected(self):
        self.save()
        node = self.data["nodes"]
        if node.is_dir():
            self._label_details_time.text = ""
            self._label_details_mime.text = ""
        else:
            self._label_details_time.text = "mtime: {} crtime: {}".format(node.mtime, node.crtime)
            self._label_details_mime.text = "mime: {}".format(node.mime)

    def _refresh(self):
        self.controller.refresh_current_dir()
        self._load_listview()

    def _item_action(self):
        self.save()
        self.controller.item_action_for(self.data["nodes"])
        self._load_listview()

    @staticmethod
    def _quit():
        raise StopApplication("User pressed quit")

class FileView(Frame):
    def __init__(self, screen, controller):
        super(FileView, self).__init__(screen,
                                       screen.height,
                                       screen.width,
                                       on_load=self._load_file,
                                       hover_focus=True,
                                       can_scroll=False,
                                       title=controller.state.current_path)
        self.controller=controller

        main_layout = Layout([1], fill_frame=True)
        self.add_layout(main_layout)
        self._text = TextBox(Widget.FILL_FRAME, readonly=True,as_string=True)
        main_layout.add_widget(self._text)
        main_layout.add_widget(Divider())

        layout2 = Layout([1,1,1,1])
        self.add_layout(layout2)
        layout2.add_widget(Button("Reload", self._reload))
        self._reload_message = Label("")
        layout2.add_widget(self._reload_message, 1)
        layout2.add_widget(Button("Back", self._back), 3)
        self.fix()

    def _load_file(self):
        self._text.value = self.controller.get_current_file()

    def _reload(self):
        self._text.value = self.controller.reload_current_file()
        self._reload_message.text = "Reloaded at: {}".format(datetime.datetime.now().isoformat(sep=' ', timespec='milliseconds'))

    def _back(self):
        self.controller.back_to_browser()


@dataclass
class TUIState:
    root_dir: Path
    parent_stack: typing.List[Path]
    current_path: Path
    directory_listings: typing.Dict[Path, Directory]

    def initial():
        return TUIState(root_dir=Path.parse("/home/"), current_path=Path.parse("/home/"), directory_listings = {}, parent_stack=[])

class TUIController:
    GOTO_PARENT=Node(path=None,name=None,href=None,crtime=None, mtime=None, mime="inode/directory",size=0)  # instantiate an object for getting a unique thing to compare.

    def __init__(self, requester, state):
        self.requester = requester
        self.state = state
        self._current_file_content = None

    def get_current_dir(self):
        cd = self.state.current_path
        dl = self.state.directory_listings

        if cd not in dl:
            dl[cd] = self.requester.list_dir(cd)

        return dl[cd]


    def in_root_dir(self):
        return self.state.current_path== self.state.root_dir

    def refresh_current_dir(self):
        cd = self.state.current_path
        dl = self.state.directory_listings

        if cd in dl:
            del dl[cd]

    def item_action_for(self, node):
        if node == TUIController.GOTO_PARENT:
            self.state.current_path = self.state.parent_stack.pop()
        elif node.is_dir():
            self.state.parent_stack.append(self.state.current_path)
            self.state.current_path = node.path
        else:
            self.state.parent_stack.append(self.state.current_path)
            self.state.current_path = node.path
            raise NextScene("Show file")

    def back_to_browser(self):
        self._current_file_content = None
        self.state.current_path = self.state.parent_stack.pop()
        raise NextScene("Browser")

    def get_current_file(self):
        if self._current_file_content is None:
            self._current_file_content = self.requester.get_file(self.state.current_path).decode("utf-8")
        return self._current_file_content

    def reload_current_file(self):
        self._current_file_content = None
        return self.get_current_file()

class TUI:
    def __init__(self, vfs_api_client):
        state = TUIState.initial()
        self._controller = TUIController(vfs_api_client, state)
    
    def start(self):
        last_scene = None
        while True:
            try:
                Screen.wrapper(self._main, arguments=[last_scene])
                sys.exit(0)
            except ResizeScreenError as e:
                last_scene = e.scene

    def _main(self, screen, scene):
        scenes = [
          Scene([BrowserView(screen, self._controller)], -1, name="Browser"),
          Scene([FileView(screen, self._controller)], -1, name="Show file")
        ]

        screen.play(scenes, stop_on_resize=True, start_scene=scene)


## CLI parsing

class Configuration:
    user = None
    password = None
    publish_url = None

    def build_vfs_client(self):
        if any([x is None for x in [self.user, self.password, self.publish_url]]):
                raise Exception("Incomplete configuration. Please set --publish-settings-file, or all of --user, --password and --publish-url")
        return VfsClient(user=self.user, password=self.password, publish_url=self.publish_url)

pass_context = click.make_pass_decorator(Configuration, ensure=True)

@click.group()
@click.option("--user")
@click.option("--password")
@click.option("--publish-url")
@click.option("--publish-settings-file",type=click.Path(exists=True))
@pass_context
def main(config, user, password, publish_url, publish_settings_file):
    # Attempt to set the configuration. Do not fail if it's not set, as this will prevent calling --help on subcommands.
    any_u_p_u_set = any((x is not None for x in [user, password, publish_url]))
    all_u_p_u_set = all((x is not None for x in [user, password, publish_url]))

    if publish_settings_file is not None:
        if any_u_p_u_set:
            raise click.UsageError("When using --publish-settings-file, you cannot specify a user, password or publish_url.")
        else:
          tree = ET.parse(publish_settings_file)
          root = tree.getroot()
          attrs = root.findall("publishProfile")[0].attrib
          config.user = attrs["userName"]
          config.password=attrs["userPWD"]
          config.publish_url ="https://" + attrs["publishUrl"]
    elif any_u_p_u_set:
        if not all_u_p_u_set:
            raise click.UsageError("When specifiying either --user, --password or --publish_url, you must specify all.")
        else:
          config.user = user
          config.password=password
          config.publish_url=publish_url


def byte_size_to_human_size(size):
    if size < 1024:
        return "{}b".format(size)
    elif size < (1024**2):
        return "{:.2f}kb".format(size/1024)
    elif size < (1024**3):
        return "{:.2f}mb".format(size/(1024**2))
    else:
        return "{:.2f}gb".format(size/(1024**3))
            
@main.command()
@click.argument("PATH", type=click.UNPROCESSED, callback=lambda ctx, param, value: Path.parse(value))
@pass_context
def get(config, path):
    """Get a file, or print a directory listing. Mark a path to be a directory by having a '/ as the last char"""
    if path.is_dir():
        d = config.build_vfs_client().list_dir(path)
        if len(d.nodes) == 0:
            print("Directory {} is empty".format(path))
        else:
            print("Content of {}, which contains {} nodes".format(d.path, len(d.nodes)))
            for node in d.nodes:
                if node.is_dir():
                    print("{}/".format(node.name))
                else:
                    print("{name} {size}".format(name=node.name, size=byte_size_to_human_size(node.size)))
    else:
        sys.stdout.buffer.write(config.build_vfs_client().get_file(path))

@main.command()
@pass_context
def tui(config):
    TUI(config.build_vfs_client()).start()

if __name__ == "__main__":
    main()
