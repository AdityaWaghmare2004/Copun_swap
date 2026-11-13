from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models import F, IntegerField, Value, When


class User(AbstractUser):
    trust_score = models.IntegerField(default=100)
    successful_trades_count = models.PositiveIntegerField(default=0)
    disputed_trades_count = models.PositiveIntegerField(default=0)
    inactivity_timeout_count = models.PositiveIntegerField(default=0)
    requires_review = models.BooleanField(default=False)

    @property
    def dispute_rate_percent(self):
        trade_count = self.successful_trades_count + self.disputed_trades_count
        if trade_count == 0:
            return None
        return self.disputed_trades_count * 100 / trade_count

    def apply_inactivity_penalty(self, heavy=False):
        deduction = 25 if heavy else 15
        type(self).objects.filter(pk=self.pk).update(
            trust_score=models.Case(
                When(
                    trust_score__gte=deduction,
                    then=F("trust_score") - deduction,
                ),
                default=Value(0),
                output_field=IntegerField(),
            ),
            inactivity_timeout_count=F("inactivity_timeout_count") + 1,
            requires_review=models.Case(
                When(inactivity_timeout_count__gte=1, then=Value(True)),
                default=F("requires_review"),
                output_field=models.BooleanField(),
            ),
        )
        self.refresh_from_db(
            fields=("trust_score", "inactivity_timeout_count", "requires_review")
        )

    def apply_success_reward(self):
        type(self).objects.filter(pk=self.pk).update(
            trust_score=models.Case(
                When(trust_score__gte=97, then=Value(100)),
                default=F("trust_score") + 3,
                output_field=IntegerField(),
            ),
            successful_trades_count=F("successful_trades_count") + 1,
        )
        self.refresh_from_db(fields=("trust_score", "successful_trades_count"))
