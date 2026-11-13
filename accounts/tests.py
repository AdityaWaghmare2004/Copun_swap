from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse


User = get_user_model()


class UserModelTests(TestCase):
    def test_reputation_fields_default_to_baseline(self):
        user = User.objects.create_user(username="trader", password="safe-password")

        self.assertEqual(user.trust_score, 100)
        self.assertEqual(user.successful_trades_count, 0)
        self.assertEqual(user.disputed_trades_count, 0)


class AccountAuthenticationTests(TestCase):
    def test_signup_creates_and_logs_in_user(self):
        response = self.client.post(
            reverse("accounts:signup"),
            {
                "username": "new-trader",
                "password1": "A-strong-password-934!",
                "password2": "A-strong-password-934!",
            },
        )

        self.assertRedirects(response, reverse("accounts:profile"))
        self.assertTrue(User.objects.filter(username="new-trader").exists())
        self.assertEqual(
            int(self.client.session["_auth_user_id"]),
            User.objects.get(username="new-trader").pk,
        )

    def test_profile_requires_authentication(self):
        response = self.client.get(reverse("accounts:profile"))

        self.assertRedirects(
            response,
            f"{reverse('accounts:login')}?next={reverse('accounts:profile')}",
        )

    def test_authenticated_user_can_view_profile(self):
        user = User.objects.create_user(username="trader", password="safe-password")
        self.client.force_login(user)

        response = self.client.get(reverse("accounts:profile"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["user"], user)

    def test_login_and_logout(self):
        User.objects.create_user(username="trader", password="safe-password")

        login_response = self.client.post(
            reverse("accounts:login"),
            {"username": "trader", "password": "safe-password"},
        )
        self.assertRedirects(login_response, reverse("accounts:profile"))

        logout_response = self.client.post(reverse("accounts:logout"))
        self.assertRedirects(logout_response, reverse("accounts:login"))
