from datetime import datetime
from decimal import Decimal, DecimalException
from urllib.parse import urlencode

import pytz
from django.conf import settings as django_settings
from django.urls import reverse
from django.views.decorators.clickjacking import xframe_options_exempt
from django.shortcuts import redirect
from django.utils.translation import gettext as _
from rest_framework.decorators import api_view, renderer_classes, parser_classes
from rest_framework.response import Response
from rest_framework.request import Request
from rest_framework.renderers import (
    TemplateHTMLRenderer,
    JSONRenderer,
    BrowsableAPIRenderer,
)
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from stellar_sdk.keypair import Keypair
from stellar_sdk.strkey import StrKey
from stellar_sdk.exceptions import (
    Ed25519PublicKeyInvalidError,
    MuxedEd25519AccountInvalidError,
    ValueError as StellarSdkValueError,
)

from polaris import settings
from polaris.templates import Template
from polaris.utils import (
    getLogger,
    render_error_response,
    extract_sep9_fields,
    create_transaction_id,
    memo_hex_to_base64,
)
from polaris.sep24.utils import (
    interactive_url,
    check_authentication,
    authenticate_session,
    invalidate_session,
    interactive_args_validation,
    get_timezone_utc_offset,
)
from polaris.sep10.utils import validate_sep10_token
from polaris.sep10.token import SEP10Token
from polaris.models import Asset, Transaction
from polaris.integrations.forms import TransactionForm
from polaris.locale.utils import validate_language, activate_lang_for_request
from polaris.integrations import (
    registered_withdrawal_integration as rwi,
    registered_fee_func,
    calculate_fee,
    registered_toml_func,
)

logger = getLogger(__name__)


@xframe_options_exempt
@api_view(["POST"])
@renderer_classes([TemplateHTMLRenderer])
@parser_classes([MultiPartParser, FormParser, JSONParser])
@check_authentication()
def post_interactive_withdraw(request: Request) -> Response:
    """
    POST /transactions/withdraw/webapp

    This endpoint processes form submissions during the withdraw interactive
    flow. The following steps are taken during this process:

        1. URL arguments are parsed and validated.
        2. form_for_transaction() is called to retrieve the form used to
           submit this request. This function is implemented by the anchor.
        3. The form is used to validate the data submitted, and if the form
           is a TransactionForm, the fee for the transaction is calculated.
        4. after_form_validation() is called to allow the anchor to process
           the data submitted. This function should change the application
           state such that the next call to form_for_transaction() returns
           the next form in the flow.
        5. form_for_transaction() is called again to retrieve the next
           form to be served to the user. If a form is returned, the
           function redirects to GET /transaction/withdraw/webapp. Otherwise,
           The user's session is invalidated, the transaction status is
           updated, and the function redirects to GET /more_info.
    """
    args_or_error = interactive_args_validation(request, Transaction.KIND.withdrawal)
    if "error" in args_or_error:
        return args_or_error["error"]

    transaction = args_or_error["transaction"]
    asset = args_or_error["asset"]
    callback = args_or_error["callback"]
    amount = args_or_error["amount"]

    form = rwi.form_for_transaction(
        request=request, transaction=transaction, post_data=request.data
    )
    if not form:
        logger.error(
            "Initial form_for_transaction() call returned None " f"for {transaction.id}"
        )
        if transaction.status != transaction.STATUS.incomplete:
            return render_error_response(
                _(
                    "The anchor did not provide content, is the interactive flow already complete?"
                ),
                status_code=422,
                content_type="text/html",
            )
        return render_error_response(
            _("The anchor did not provide content, unable to serve page."),
            status_code=500,
            content_type="text/html",
        )

    if not form.is_bound:
        # The anchor must initialize the form with request.data
        logger.error("form returned was not initialized with POST data, returning 500")
        return render_error_response(
            _("Unable to validate form submission."),
            status_code=500,
            content_type="text/html",
        )

    elif form.is_valid():
        if issubclass(form.__class__, TransactionForm):
            transaction.amount_in = form.cleaned_data["amount"]
            transaction.amount_expected = form.cleaned_data["amount"]
            try:
                transaction.amount_fee = registered_fee_func(
                    request=request,
                    fee_params={
                        "amount": transaction.amount_in,
                        "type": form.cleaned_data.get("type"),
                        "operation": settings.OPERATION_WITHDRAWAL,
                        "asset_code": asset.code,
                    },
                )
            except ValueError:
                pass
            else:
                if settings.ADDITIVE_FEES_ENABLED:
                    transaction.amount_in += transaction.amount_fee
                    transaction.amount_expected += transaction.amount_fee
                transaction.amount_out = round(
                    transaction.amount_in - transaction.amount_fee,
                    asset.significant_decimals,
                )
            transaction.save()

        try:
            rwi.after_form_validation(
                request=request, form=form, transaction=transaction
            )
        except NotImplementedError:
            pass
        next_form = rwi.form_for_transaction(request=request, transaction=transaction)
        try:
            next_content = rwi.content_for_template(
                request=request,
                template=Template.WITHDRAW,
                form=next_form,
                transaction=transaction,
            )
        except NotImplementedError:
            next_content = None
        if next_form or next_content:
            args = {"transaction_id": transaction.id, "asset_code": asset.code}
            if amount:
                args["amount"] = amount
            if callback:
                args["callback"] = callback
            url = reverse("get_interactive_withdraw")
            return redirect(f"{url}?{urlencode(args)}")

        else:  # Last form has been submitted
            logger.info(
                f"Finished data collection and processing for transaction {transaction.id}"
            )
            invalidate_session(request)

            # ensure all changes from `after_form_validation()` have been saved to the db
            transaction.refresh_from_db()

            # Add memo now that interactive flow is complete
            #
            # We use the transaction ID as a memo on the Stellar transaction for the
            # payment in the withdrawal. This lets us identify that as uniquely
            # corresponding to this `Transaction` in the database. But a UUID4 is a 32
            # character hex string, while the Stellar HashMemo requires a 64 character
            # hex-encoded (32 byte) string. So, we zero-pad the ID to create an
            # appropriately sized string for the `HashMemo`.
            transaction_id_hex = transaction.id.hex
            padded_hex_memo = "0" * (64 - len(transaction_id_hex)) + transaction_id_hex
            transaction.memo = memo_hex_to_base64(padded_hex_memo)

            if transaction.status != transaction.STATUS.pending_anchor:
                transaction.status = Transaction.STATUS.pending_user_transfer_start
            else:
                logger.info(f"Transaction {transaction.id} is pending KYC approval")
            transaction.save()
            args = {"id": transaction.id, "initialLoad": "true"}
            if callback:
                args["callback"] = callback
            return redirect(f"{reverse('more_info')}?{urlencode(args)}")

    else:
        try:
            content_from_anchor = (
                rwi.content_for_template(
                    request=request,
                    template=Template.WITHDRAW,
                    form=form,
                    transaction=transaction,
                )
                or {}
            )
        except NotImplementedError:
            content_from_anchor = {}

        url_args = {"transaction_id": transaction.id, "asset_code": asset.code}
        if callback:
            url_args["callback"] = callback
        if amount:
            url_args["amount"] = amount

        current_offset = get_timezone_utc_offset(
            request.session.get("timezone") or django_settings.TIME_ZONE
        )
        toml_data = registered_toml_func(request=request)
        post_url = f"{reverse('post_interactive_withdraw')}?{urlencode(url_args)}"
        content = {
            "form": form,
            "post_url": post_url,
            "operation": settings.OPERATION_WITHDRAWAL,
            "asset": asset,
            "symbol": asset.symbol,
            "show_fee_table": isinstance(form, TransactionForm),
            "use_fee_endpoint": registered_fee_func != calculate_fee,
            "org_logo_url": toml_data.get("DOCUMENTATION", {}).get("ORG_LOGO"),
            "additive_fees_enabled": settings.ADDITIVE_FEES_ENABLED,
            "timezone_endpoint": reverse("tzinfo"),
            "session_id": request.session.session_key,
            "current_offset": current_offset,
            **content_from_anchor,
        }
        return Response(
            content,
            template_name=content_from_anchor.get(
                "template_name", "polaris/withdraw.html"
            ),
            status=400,
        )


@api_view(["GET"])
@renderer_classes([TemplateHTMLRenderer])
@check_authentication()
def complete_interactive_withdraw(request: Request) -> Response:
    """
    GET /transactions/withdraw/interactive/complete

    Updates the transaction status to pending_user_transfer_start and
    redirects to GET /more_info. A `callback` can be passed in the URL
    to be used by the more_info template javascript.
    """
    transaction_id = request.GET.get("transaction_id")
    callback = request.GET.get("callback")
    Transaction.objects.filter(id=transaction_id).update(
        status=Transaction.STATUS.pending_user_transfer_start
    )
    logger.info(f"Hands-off interactive flow complete for transaction {transaction_id}")
    args = {"id": transaction_id, "initialLoad": "true"}
    if callback:
        args["callback"] = callback
    return redirect(f"{reverse('more_info')}?{urlencode(args)}")


@xframe_options_exempt
@api_view(["GET"])
@renderer_classes([TemplateHTMLRenderer])
@authenticate_session()
def get_interactive_withdraw(request: Request) -> Response:
    """
    GET /transactions/withdraw/webapp

    This endpoint retrieves the next form to be served to the user in the
    interactive flow. The following steps are taken during this process:

        1. URL arguments are parsed and validated.
        2. interactive_url() is called to determine whether or not the anchor
           uses an external service for the interactive flow. If a URL is
           returned, this function redirects to the URL. However, the session
           cookie should still be included in the response so future calls to
           GET /transactions/withdraw/interactive/complete are authenticated.
        3. form_for_transaction() is called to retrieve the next form to
           render to the user.
        4. get and post URLs are constructed with the appropriate arguments
           and passed to the response to be rendered to the user.
    """
    args_or_error = interactive_args_validation(request, Transaction.KIND.withdrawal)
    if "error" in args_or_error:
        return args_or_error["error"]

    transaction = args_or_error["transaction"]
    asset = args_or_error["asset"]
    callback = args_or_error["callback"]
    amount = args_or_error["amount"]
    if args_or_error["on_change_callback"]:
        transaction.on_change_callback = args_or_error["on_change_callback"]
        transaction.save()

    try:
        url = rwi.interactive_url(
            request=request,
            transaction=transaction,
            asset=asset,
            amount=amount,
            callback=callback,
        )
    except NotImplementedError:
        pass
    else:
        # The anchor uses a standalone interactive flow
        return redirect(url)

    form = rwi.form_for_transaction(
        request=request, transaction=transaction, amount=amount
    )
    try:
        content_from_anchor = (
            rwi.content_for_template(
                request=request,
                template=Template.WITHDRAW,
                form=form,
                transaction=transaction,
            )
            or {}
        )
    except NotImplementedError:
        content_from_anchor = {}

    if not (form or content_from_anchor):
        logger.error("The anchor did not provide content, unable to serve page.")
        if transaction.status != transaction.STATUS.incomplete:
            return render_error_response(
                _(
                    "The anchor did not provide content, is the interactive flow already complete?"
                ),
                status_code=422,
                content_type="text/html",
            )
        return render_error_response(
            _("The anchor did not provide content, unable to serve page."),
            status_code=500,
            content_type="text/html",
        )

    url_args = {"transaction_id": transaction.id, "asset_code": asset.code}
    if callback:
        url_args["callback"] = callback
    if amount:
        url_args["amount"] = amount

    current_offset = get_timezone_utc_offset(
        request.session.get("timezone") or django_settings.TIME_ZONE
    )
    post_url = f"{reverse('post_interactive_withdraw')}?{urlencode(url_args)}"
    toml_data = registered_toml_func(request=request)
    content = {
        "form": form,
        "post_url": post_url,
        "operation": settings.OPERATION_WITHDRAWAL,
        "asset": asset,
        "symbol": asset.symbol,
        "show_fee_table": isinstance(form, TransactionForm),
        "use_fee_endpoint": registered_fee_func != calculate_fee,
        "org_logo_url": toml_data.get("DOCUMENTATION", {}).get("ORG_LOGO"),
        "additive_fees_enabled": settings.ADDITIVE_FEES_ENABLED,
        "timezone_endpoint": reverse("tzinfo"),
        "session_id": request.session.session_key,
        "current_offset": current_offset,
        **content_from_anchor,
    }

    return Response(
        content,
        template_name=content_from_anchor.get("template_name", "polaris/withdraw.html"),
    )


@api_view(["POST"])
@validate_sep10_token()
@renderer_classes([JSONRenderer, BrowsableAPIRenderer])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def withdraw(token: SEP10Token, request: Request,) -> Response:
    """
    POST /transactions/withdraw/interactive

    Creates an `incomplete` withdraw Transaction object in the database and
    returns the URL entry-point for the interactive flow.
    """
    lang = request.data.get("lang")
    asset_code = request.data.get("asset_code")
    source_account = request.data.get("account")
    sep9_fields = extract_sep9_fields(request.data)
    if lang:
        err_resp = validate_language(lang)
        if err_resp:
            return err_resp
        activate_lang_for_request(lang)
    if not asset_code:
        return render_error_response(_("'asset_code' is required"))

    # Verify that the asset code exists in our database, with withdraw enabled.
    asset = Asset.objects.filter(code=asset_code).first()
    if not (
        asset
        and asset.withdrawal_enabled
        and asset.sep24_enabled
        and asset.distribution_account
    ):
        return render_error_response(_("invalid operation for asset %s") % asset_code)

    amount = None
    if request.data.get("amount"):
        try:
            amount = Decimal(request.data.get("amount"))
        except DecimalException:
            return render_error_response(_("invalid 'amount'"))
        if not (asset.withdrawal_min_amount <= amount <= asset.withdrawal_max_amount):
            return render_error_response(_("invalid 'amount'"))

    if source_account and source_account.startswith("M"):
        try:
            StrKey.decode_muxed_account(source_account)
        except (MuxedEd25519AccountInvalidError, StellarSdkValueError):
            return render_error_response(_("invalid 'account'"))
    elif source_account:
        try:
            Keypair.from_public_key(source_account)
        except Ed25519PublicKeyInvalidError:
            return render_error_response(_("invalid 'account'"))

    if sep9_fields:
        try:
            rwi.save_sep9_fields(
                token=token,
                request=request,
                stellar_account=token.account,
                muxed_account=token.muxed_account,
                account_memo=str(token.memo) if token.memo else None,
                account_memo_type=Transaction.MEMO_TYPES.id if token.memo else None,
                fields=sep9_fields,
                language_code=lang,
            )
        except ValueError as e:
            # The anchor found a validation error in the sep-9 fields POSTed by
            # the wallet. The error string returned should be in the language
            # specified in the request.
            return render_error_response(str(e))
        except NotImplementedError:
            # the KYC info passed via SEP-9 fields can be ignored if the anchor
            # wants to re-collect the information
            pass

    transaction_id = create_transaction_id()
    Transaction.objects.create(
        id=transaction_id,
        stellar_account=token.account,
        muxed_account=token.muxed_account,
        account_memo=token.memo,
        asset=asset,
        kind=Transaction.KIND.withdrawal,
        status=Transaction.STATUS.incomplete,
        receiving_anchor_account=asset.distribution_account,
        memo_type=Transaction.MEMO_TYPES.hash,
        protocol=Transaction.PROTOCOL.sep24,
        more_info_url=request.build_absolute_uri(
            f"{reverse('more_info')}?id={transaction_id}"
        ),
        client_domain=token.client_domain,
        from_address=source_account,
    )
    logger.info(f"Created withdrawal transaction {transaction_id}")

    url = interactive_url(
        request=request,
        transaction_id=str(transaction_id),
        account=token.muxed_account or token.account,
        memo=token.memo,
        asset_code=asset_code,
        op_type=settings.OPERATION_WITHDRAWAL,
        amount=amount,
    )
    return Response(
        {"type": "interactive_customer_info_needed", "url": url, "id": transaction_id}
    )
