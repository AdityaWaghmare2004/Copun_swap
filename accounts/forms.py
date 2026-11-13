from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm


class SignUpForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = get_user_model()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update(
                {
                    "class": (
                        "block w-full rounded-lg border border-slate-300 bg-white "
                        "px-3 py-2.5 text-slate-900 shadow-sm placeholder:text-slate-400 "
                        "focus:border-sky-600 focus:outline-none focus:ring-2 "
                        "focus:ring-sky-100"
                    ),
                    "autocapitalize": "none",
                    "autocomplete": (
                        "new-password" if field.widget.input_type == "password" else "username"
                    ),
                }
            )
