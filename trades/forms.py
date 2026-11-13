from django import forms

from coupons.models import Coupon


class TradeProposalForm(forms.Form):
    offered_coupon = forms.ModelChoiceField(
        queryset=Coupon.objects.none(),
        label="Your coupon",
    )

    def __init__(self, *args, sender, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["offered_coupon"].queryset = Coupon.objects.filter(
            owner=sender,
            status=Coupon.Status.AVAILABLE,
        ).only("pk", "owner_id", "title", "expiry_date").order_by(
            "expiry_date",
            "title",
        )
