from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.urls import reverse_lazy
from django.views.generic import CreateView
from django.shortcuts import render

from .forms import SignUpForm


class SignUpView(CreateView):
    form_class = SignUpForm
    template_name = "accounts/signup.html"
    success_url = reverse_lazy("accounts:profile")

    def form_valid(self, form):
        response = super().form_valid(form)
        login(self.request, self.object)
        return response


@login_required
def profile(request):
    return render(request, "accounts/profile.html")
