from gi.repository import GLib, Gtk


def set_status_label_markup(label, color):
    colors = {
        "red": "#ff0000",
        "green": "#00b300",
        "orange": "#ff8c00",
    }
    current_text = label.get_text() or ""
    safe_text = GLib.markup_escape_text(current_text)
    label.set_markup(
        f'<span foreground="{colors.get(color, colors["red"])}">{safe_text}</span>'
    )


def show_message(parent, message_type, text):
    dialog = Gtk.MessageDialog(
        parent=parent,
        modal=True,
        message_type=message_type,
        buttons=Gtk.ButtonsType.OK,
        text=text,
    )
    dialog.run()
    dialog.destroy()
