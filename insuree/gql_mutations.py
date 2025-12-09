import logging
from uuid import uuid4, UUID
import pathlib
import base64
import json
import graphene
from insuree.apps import InsureeConfig
from insuree.services import validate_insuree_number, InsureeService, FamilyService, InsureePolicyService, InsureeIdReservationService
from django.conf import settings

from core.schema import (
    OpenIMISMutation,
    OpenIMISJSONEncoder,
    signal_mutation,
    signal_mutation_module_validate,
    signal_mutation_module_before_mutating,
    signal_mutation_module_after_mutating,
)
from core.models import MutationLog, Language
from django.utils import translation
from django.middleware.csrf import CsrfViewMiddleware
from core.utils import is_this_session_superuser
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError, PermissionDenied
from django.utils.translation import gettext as _
from graphene import InputObjectType
from .models import Family, Insuree, FamilyMutation, InsureeMutation, DisabilityStatus, InsureeCheckIn

logger = logging.getLogger(__name__)


class PhotoInputType(InputObjectType):
    id = graphene.Int(required=False, read_only=True)
    uuid = graphene.String(required=False)
    date = graphene.Date(required=False)
    officer_id = graphene.Int(required=False)
    photo = graphene.String(required=False)
    filename = graphene.String(required=False)
    folder = graphene.String(required=False)


class InsureeBase:
    id = graphene.Int(required=False, read_only=True)
    uuid = graphene.String(required=False)
    chf_id = graphene.String(max_length=50, required=False)
    last_name = graphene.String(max_length=100, required=True)
    middle_name = graphene.String(max_length=100, required=False)
    other_names = graphene.String(max_length=100, required=True)
    gender_id = graphene.String(max_length=1, required=True)
    dob = graphene.Date(required=True)
    head = graphene.Boolean(required=False)
    marital = graphene.String(max_length=1, required=False)
    passport = graphene.String(max_length=25, required=False)
    phone = graphene.String(max_length=50, required=False)
    email = graphene.String(max_length=100, required=False)
    current_address = graphene.String(max_length=200, required=False)
    geolocation = graphene.String(max_length=250, required=False)
    current_village_id = graphene.Int(required=False)
    photo_id = graphene.Int(required=False)
    photo_date = graphene.Date(required=False)
    photo = graphene.Field(PhotoInputType, required=False)
    disability_status = graphene.String(
        description="Disability status of the insuree",
        required=False
    )
    card_issued = graphene.Boolean(required=False)
    family_id = graphene.Int(required=False)
    relationship_id = graphene.Int(required=False)
    profession_id = graphene.Int(required=False)
    education_id = graphene.Int(required=False)
    type_of_id_id = graphene.String(max_length=1, required=False)
    health_facility_id = graphene.Int(required=False)
    offline = graphene.Boolean(required=False)
    json_ext = graphene.types.json.JSONString(required=False)
    status = graphene.String(required=False)
    status_reason = graphene.String(required=False)
    status_date = graphene.Date(required=False)
    chf_id_format = graphene.Int(required=False, description="1=region/district, 2=district, 3=none")
    add_on_existing_policy = graphene.Boolean(required=False)
    is_active = graphene.Boolean(required=False, description="Whether the insuree is active")
    # If provided, the insuree will be created with this pre-reserved CHFID
    reserved_chf_id = graphene.String(required=False, description="Use a pre-reserved CHFID for creation")


class CreateInsureeInputType(InsureeBase, OpenIMISMutation.Input):
    pass


class UpdateInsureeInputType(InsureeBase, OpenIMISMutation.Input):
    no_versioning = graphene.Boolean(required=False, description="Update current row in place without creating history")


class FamilyHeadInsureeInputType(InsureeBase, InputObjectType):
    pass


class FamilyBase:
    id = graphene.Int(required=False, read_only=True)
    uuid = graphene.String(required=False)
    location_id = graphene.Int()
    poverty = graphene.Boolean(required=False)
    family_type_id = graphene.String(max_length=1, required=False)
    address = graphene.String(max_length=200, required=False)
    is_offline = graphene.Boolean(required=False)
    ethnicity = graphene.String(max_length=1, required=False)
    confirmation_no = graphene.String(max_length=12, required=False)
    confirmation_type_id = graphene.String(max_length=3, required=False)
    json_ext = graphene.types.json.JSONString(required=False)

    contribution = graphene.types.json.JSONString(required=False)

    head_insuree = graphene.Field(FamilyHeadInsureeInputType, required=False)


class FamilyInputType(FamilyBase, OpenIMISMutation.Input):
    pass


class CreateFamilyInputType(FamilyInputType):
    pass
class CheckInBase:
    uuid = graphene.String(required=True, read_only=True)

class InsureeCheckInInputType(CheckInBase , OpenIMISMutation.Input):
    pass


class UpdateFamilyInputType(FamilyInputType):
    pass



def update_or_create_insuree(data, user):
    data.pop('client_mutation_id', None)
    data.pop('client_mutation_label', None)
    return InsureeService(user).create_or_update(data)


def update_or_create_family(data, user):
    data.pop('client_mutation_id', None)
    data.pop('client_mutation_label', None)
    return FamilyService(user).create_or_update(data)


class CreateFamilyMutation(OpenIMISMutation):
    """
    Create a new family, with its head insuree
    """
    _mutation_module = "insuree"
    _mutation_class = "CreateFamilyMutation"

    class Input(CreateFamilyInputType):
        pass

    @classmethod
    def async_mutate(cls, user, **data):
        try:
            if type(user) is AnonymousUser or not user.id:
                raise ValidationError(
                    _("mutation.authentication_required"))
            if not user.has_perms(InsureeConfig.gql_mutation_create_families_perms):
                raise PermissionDenied(_("unauthorized"))
            data['audit_user_id'] = user.id_for_audit
            from core.utils import TimeUtils
            data['validity_from'] = TimeUtils.now()
            client_mutation_id = data.get("client_mutation_id")
            family = update_or_create_family(data, user)
            FamilyMutation.object_mutated(
                user, client_mutation_id=client_mutation_id, family=family)
            return None
        except Exception as exc:
            logger.exception("insuree.mutation.failed_to_create_family")
            return [{
                'message': _("insuree.mutation.failed_to_create_family"),
                'detail': str(exc)}
            ]
class InsureeCheckInMutation(OpenIMISMutation):
    """
    Check in an insuree
    """
    _mutation_module = "insuree"
    _mutation_class = "InsureeCheckInMutation"

    class Input(InsureeCheckInInputType):
        pass

    @classmethod
    def async_mutate(cls, user, **data):
        try:
            if type(user) is AnonymousUser or not user.id:
                raise ValidationError(
                    _("mutation.authentication_required"))
            if not user.has_perms(InsureeConfig.gql_mutation_checkin_insuree_perms):
                raise PermissionDenied(_("unauthorized"))
            if user.health_facility is None:
                raise ValidationError(
                    "Receptionist accounts must be assigned to a Health Facility before they can perform insuree check-ins..")
            client_mutation_id = data.get("client_mutation_id")
            InsureeService(user).checkin_insuree(data)
            InsureeMutation.object_mutated(
                user, client_mutation_id=client_mutation_id)
            return None
        except Exception as exc:
            logger.exception("insuree.mutation.failed_to_create_family")
            return [{
                'message': "Failed to check in insuree",
                'detail': str(exc)}
            ]


class ReserveInsureeIdsMutation(OpenIMISMutation):
    """Reserve a batch of CHFIDs for the current user/officer and HF."""
    _mutation_module = "insuree"
    _mutation_class = "ReserveInsureeIdsMutation"

    class Input(OpenIMISMutation.Input):
        amount = graphene.Int(required=True)
        autogenerate = graphene.Boolean(required=False)

    reserved_chf_ids = graphene.List(graphene.String)

    @classmethod
    def async_mutate(cls, user, **data):
        try:
            if type(user) is AnonymousUser or not user.id:
                raise ValidationError(_("mutation.authentication_required"))
            # Reuse create permission for reserving IDs
            if not user.has_perms(InsureeConfig.gql_mutation_create_insurees_perms):
                raise PermissionDenied(_("unauthorized"))
            amount = int(data.get('amount') or 0)
            reserved = InsureeIdReservationService(user).reserve_new(amount)
            # If client asks for autogenerate, return fields to surface in payload
            # Otherwise, follow default contract and return None
            logger.info("Reserved %s CHFIDs for user %s", len(reserved), getattr(user, 'username', None))
            if data.get('autogenerate', False):
                return { 'reserved_chf_ids': reserved }
            return None
        except Exception as exc:
            logger.exception("insuree.mutation.failed_to_reserve_ids")
            return [{
                'message': _("insuree.mutation.failed_to_reserve_ids"),
                'detail': str(exc)}
            ]

    @classmethod
    def mutate_and_get_payload(cls, root, info, **data):
        request = getattr(info, "context", None)

        user_agent = request.headers.get("User-Agent", "")
        current_session_key = request.session.session_key
        if not is_this_session_superuser(current_session_key):
            if not any(bypass in user_agent for bypass in getattr(settings, "USER_AGENT_CSRF_BYPASS", [])):
                csrf_middleware = CsrfViewMiddleware(lambda req: None)
                reason = csrf_middleware.process_view(request, None, (), {})
                if reason:
                    raise PermissionDenied("CSRF token missing or incorrect.")

        mutation_log = MutationLog.objects.create(
            json_content=json.dumps(data, cls=OpenIMISJSONEncoder),
            user_id=info.context.user.id if info.context.user else None,
            client_mutation_id=data.get("client_mutation_id"),
            client_mutation_label=data.get("client_mutation_label"),
            client_mutation_details=json.dumps(
                data.get("client_mutation_details"), cls=OpenIMISJSONEncoder
            )
            if data.get("client_mutation_details")
            else None,
        )
        logger.debug(
            "OpenIMISMutation: saved as %s, type: %s, label: %s",
            mutation_log.id,
            cls.__name__,
            mutation_log.client_mutation_label,
        )
        if (
            info
            and info.context
            and info.context.user
            and not info.context.user.is_anonymous
        ):
            lang = info.context.user.language
            if isinstance(lang, Language):
                translation.activate(lang.code)
            else:
                translation.activate(lang)

        error_messages = None
        generated_fields = {}
        try:
            logger.debug("[OpenIMISMutation %s] Sending signals", mutation_log.id)
            results = signal_mutation.send(
                sender=cls,
                mutation_log_id=mutation_log.id,
                data=data,
                user=info.context.user,
                mutation_module=cls._mutation_module,
                mutation_class=cls.__name__,
            )
            results.extend(
                signal_mutation_module_validate[cls._mutation_module].send(
                    sender=cls,
                    mutation_log_id=mutation_log.id,
                    data=data,
                    user=info.context.user,
                    mutation_module=cls._mutation_module,
                    mutation_class=cls.__name__,
                )
            )
            errors = [err for r in results for err in r[1]]
            logger.debug(
                "[OpenIMISMutation %s] signals sent, got errors back: %d",
                mutation_log.id,
                len(errors),
            )
            if errors:
                mutation_log.mark_as_failed(json.dumps(errors))
                return cls(internal_id=mutation_log.id)

            signal_mutation_module_before_mutating[cls._mutation_module].send(
                sender=cls, mutation_log_id=mutation_log.id, data=data, user=info.context.user,
                mutation_module=cls._mutation_module, mutation_class=cls.__name__
            )
            logger.debug("[OpenIMISMutation %s] before mutate signal sent", mutation_log.id)
            # Only synchronous path considered here (mirrors core setting)
            logger.debug("[OpenIMISMutation %s] mutating...", mutation_log.id)
            try:
                from core.schema import OpenIMISJSONEncoder as _Encoder
                mutation_data = cls.coerce_mutation_data(json.loads(
                    json.dumps(data, cls=_Encoder)))
                mutation_data.pop("mutation_extensions", None)
                messages = cls.async_mutate(
                    info.context.user if info.context and info.context.user else None,
                    **mutation_data)
                if mutation_data.get('autogenerate', False) and isinstance(messages, dict):
                    # capture generated fields for response
                    generated_fields.update(messages)
                    error_messages = None
                else:
                    error_messages = messages
                if not error_messages:
                    logger.debug("[OpenIMISMutation %s] marked as successful", mutation_log.id)
                    mutation_log.mark_as_successful()
                else:
                    exceptions = [message.pop("exc") for message in error_messages if "exc" in message]
                    errors_json = json.dumps(error_messages)
                    logger.error("[OpenIMISMutation %s] marked as failed: %s", mutation_log.id, errors_json)
                    for exc in exceptions:
                        logger.error("[OpenIMISMutation %s] Exception:", mutation_log.id, exc_info=exc)
                    mutation_log.mark_as_failed(errors_json)
            except BaseException as exc:
                error_messages = exc
                logger.error("async_mutate threw an exception. It should have gotten this far.", exc_info=exc)
                mutation_log.mark_as_failed(f"The mutation threw a {type(exc)}, check logs for details")
            logger.debug("[OpenIMISMutation %s] send post mutation signal", mutation_log.id)
            signal_mutation_module_after_mutating[cls._mutation_module].send(
                sender=cls, mutation_log_id=mutation_log.id, data=data, user=info.context.user,
                mutation_module=cls._mutation_module, mutation_class=cls.__name__,
                error_messages=error_messages
            )
        except Exception as exc:
            logger.error(f"Exception while processing mutation id {mutation_log.id}", exc_info=exc)
            mutation_log.mark_as_failed(exc)

        # Return instance including any generated fields so GraphQL can resolve them
        instance = cls(internal_id=mutation_log.id)
        if generated_fields.get('reserved_chf_ids') is not None:
            setattr(instance, 'reserved_chf_ids', generated_fields.get('reserved_chf_ids'))
        return instance


class DeleteReservedInsureeIdsMutation(OpenIMISMutation):
    """Cancel reservations for the given CHFIDs owned by current user scope."""
    _mutation_module = "insuree"
    _mutation_class = "DeleteReservedInsureeIdsMutation"

    class Input(OpenIMISMutation.Input):
        chf_ids = graphene.List(graphene.String, required=True)

    deleted_count = graphene.Int()

    @classmethod
    def async_mutate(cls, user, **data):
        try:
            if type(user) is AnonymousUser or not user.id:
                raise ValidationError(_("mutation.authentication_required"))
            if not user.has_perms(InsureeConfig.gql_mutation_create_insurees_perms):
                raise PermissionDenied(_("unauthorized"))
            chf_ids = data.get('chf_ids') or []
            deleted = InsureeIdReservationService(user).delete_reserved(chf_ids)
            logger.info("Deleted %s reserved CHFIDs for user %s", deleted, getattr(user, 'username', None))
            return None
        except Exception as exc:
            logger.exception("insuree.mutation.failed_to_delete_reserved_ids")
            return [{
                'message': _("insuree.mutation.failed_to_delete_reserved_ids"),
                'detail': str(exc)}
            ]


class GetMyReservedInsureeIdsMutation(OpenIMISMutation):
    """Return reserved and used CHFIDs under current user's HF/officer scope."""
    _mutation_module = "insuree"
    _mutation_class = "GetMyReservedInsureeIdsMutation"

    class Input(OpenIMISMutation.Input):
        pass

    reserved = graphene.List(graphene.String)
    used = graphene.List(graphene.String)

    @classmethod
    def async_mutate(cls, user, **data):
        try:
            if type(user) is AnonymousUser or not user.id:
                raise ValidationError(_("mutation.authentication_required"))
            if not user.has_perms(InsureeConfig.gql_mutation_create_insurees_perms):
                raise PermissionDenied(_("unauthorized"))
            qs = InsureeIdReservationService(user).get_my()
            reserved = list(qs.filter(status="RS").values_list('chf_id', flat=True))
            used = list(qs.filter(status="US").values_list('chf_id', flat=True))
            # Success: return None, consumers should use dedicated query resolvers instead
            logger.info("Fetched reserved (%s) and used (%s) CHFIDs for user %s", len(reserved), len(used), getattr(user, 'username', None))
            return None
        except Exception as exc:
            logger.exception("insuree.mutation.failed_to_get_reserved_ids")
            return [{
                'message': _("insuree.mutation.failed_to_get_reserved_ids"),
                'detail': str(exc)}
            ]


class UpdateFamilyMutation(OpenIMISMutation):
    """
    Update an existing family, with its head insuree
    """
    _mutation_module = "insuree"
    _mutation_class = "UpdateFamilyMutation"

    class Input(UpdateFamilyInputType):
        pass

    @classmethod
    def async_mutate(cls, user, **data):
        try:
            if type(user) is AnonymousUser or not user.id:
                raise ValidationError(
                    _("mutation.authentication_required"))
            if not user.has_perms(InsureeConfig.gql_mutation_update_families_perms):
                raise PermissionDenied(_("unauthorized"))
            # In DEBUG with anonymous, avoid accessing id_for_audit
            if user.is_anonymous and getattr(settings, 'DEBUG', False):
                data['audit_user_id'] = 0
            else:
                data['audit_user_id'] = user.id_for_audit
            client_mutation_id = data.get("client_mutation_id")
            family = update_or_create_family(data, user)
            FamilyMutation.object_mutated(
                user, client_mutation_id=client_mutation_id, family=family)
            return None
        except Exception as exc:
            logger.exception("insuree.mutation.failed_to_update_family")
            return [{
                'message': _("insuree.mutation.failed_to_update_family"),
                'detail': str(exc)}
            ]


class DeleteFamiliesMutation(OpenIMISMutation):
    """
    Delete one or several families (and all its insurees).
    """
    _mutation_module = "insuree"
    _mutation_class = "DeleteFamiliesMutation"

    class Input(OpenIMISMutation.Input):
        uuids = graphene.List(graphene.String)
        delete_members = graphene.Boolean(required=False, default_value=False)

    @classmethod
    def async_mutate(cls, user, **data):
        if not user.has_perms(InsureeConfig.gql_mutation_delete_families_perms):
            raise PermissionDenied(_("unauthorized"))
        errors = []
        for family_uuid in data["uuids"]:
            family = Family.objects \
                .prefetch_related('members') \
                .filter(uuid=(family_uuid)) \
                .first()
            if family is None:
                errors.append({
                    'title': family_uuid,
                    'list': [{'message': _("insuree.mutation.failed_to_delete_family") % {'uuid': family_uuid}}]
                })
                continue
            errors += FamilyService(user).set_deleted(family,
                                                      data["delete_members"])
        if len(errors) == 1:
            errors = errors[0]['list']
        return errors


class CreateInsureeMutation(OpenIMISMutation):
    """
    Create a new insuree
    """
    _mutation_module = "insuree"
    _mutation_class = "CreateInsureeMutation"

    class Input(CreateInsureeInputType):
        pass

    @classmethod
    def async_mutate(cls, user, **data):
        try:
            if type(user) is AnonymousUser or not user.id:
                # Allow local testing when DEBUG=True, otherwise enforce auth
                if not getattr(settings, 'DEBUG', False):
                    raise ValidationError(_("mutation.authentication_required"))
                logger.warning("DEBUG: bypassing authentication for CreateInsureeMutation")
            # Only enforce permission checks for authenticated users
            if not user.is_anonymous and not user.has_perms(InsureeConfig.gql_mutation_create_insurees_perms):
                raise PermissionDenied(_("unauthorized"))
            # In DEBUG with anonymous, avoid accessing id_for_audit
            if user.is_anonymous and getattr(settings, 'DEBUG', False):
                data['audit_user_id'] = 0
            else:
                data['audit_user_id'] = user.id_for_audit
            from core.utils import TimeUtils
            data['validity_from'] = TimeUtils.now()
            client_mutation_id = data.get("client_mutation_id")
            
            # Remember if isActive was set to false
            is_active_was_false = data.get('is_active') is False
            
            # Always derive health facility from the authenticated admin (iUser),
            # ignoring any incoming value from the mutation.
            # This ensures FSP is tied to the registering admin's facility.
            try:
                i_user = getattr(user, 'i_user', None)
                hf_id = None
                if i_user is not None:
                    # Django exposes FK integer via <field>_id
                    hf_id = getattr(i_user, 'health_facility_id', None)
                # If FSP is mandatory, ensure we have it from the user context
                if getattr(InsureeConfig, 'insuree_fsp_mandatory', False) and not hf_id:
                    raise ValidationError(_("mutation.insuree.fsp_required"))
                if hf_id:
                    # Overwrite any provided value from the payload
                    data['health_facility_id'] = hf_id
                else:
                    # If not mandatory and admin has no HF, drop any provided value to avoid misuse
                    data.pop('health_facility_id', None)
            except ValidationError:
                # Re-raise validation errors to be handled by the outer except
                raise
            except Exception as e:
                logger.warning(f"Failed to derive health facility from user context: {e}")
                # Fall back: if mandatory, the service will catch missing FSP and raise
            
            # Create the insuree
            insuree = update_or_create_insuree(data, user)
            
            # If isActive was false, directly update the insuree status in the database
            if is_active_was_false:
                from insuree.models import Insuree, InsureeStatus
                try:
                    logger.info(f"Mutation handler: Setting insuree {insuree.id} to inactive status")
                    # Update both status and is_active fields directly in the database
                    Insuree.objects.filter(id=insuree.id).update(
                        status=InsureeStatus.INACTIVE,
                        is_active=False
                    )
                    # Refresh the insuree from the database
                    insuree.refresh_from_db()
                    logger.info(f"Mutation handler: Insuree status after update: {insuree.status}, is_active: {insuree.is_active}")
                except Exception as e:
                    logger.error(f"Mutation handler: Failed to set insuree {insuree.id} to inactive status: {e}")
            
            # Avoid passing AnonymousUser to mutation logger (can cause UUID error)
            mutation_user = None if (user.is_anonymous and getattr(settings, 'DEBUG', False)) else user
            InsureeMutation.object_mutated(
                mutation_user, client_mutation_id=client_mutation_id, insuree=insuree)
            return None
        except ValidationError as exc:
            # Surface specific reserved CHFID errors clearly to the client
            details = getattr(exc, 'messages', None)
            detail_text = "; ".join(details) if details else str(exc)
            if 'reserved_id_already_used' in detail_text:
                return [{
                    'message': _("reserved_id_already_used")
                }]
            if 'reserved_id_not_available' in detail_text:
                return [{
                    'message': _("reserved_id_not_available")
                }]
            if 'reserved_id_wrong_scope' in detail_text:
                return [{
                    'message': _("reserved_id_wrong_scope")
                }]
            # Fallback to generic failure with details
            logger.exception("insuree.mutation.failed_to_create_insuree")
            return [{
                'message': _("insuree.mutation.failed_to_create_insuree"),
                'detail': detail_text}
            ]
        except Exception as exc:
            logger.exception("insuree.mutation.failed_to_create_insuree")
            return [{
                'message': _("insuree.mutation.failed_to_create_insuree"),
                'detail': str(exc)}
            ]


class UpdateInsureeMutation(OpenIMISMutation):
    """
    Update an existing insuree
    """
    _mutation_module = "insuree"
    _mutation_class = "UpdateInsureeMutation"

    class Input(UpdateInsureeInputType):
        pass

    @classmethod
    def async_mutate(cls, user, **data):
        try:
            if type(user) is AnonymousUser or not user.id:
                # Allow local testing when DEBUG=True, otherwise enforce auth
                if not getattr(settings, 'DEBUG', False):
                    raise ValidationError(_("mutation.authentication_required"))
                logger.warning("DEBUG: bypassing authentication for UpdateInsureeMutation")
            # Use update permission for update mutation
            if not user.is_anonymous and not user.has_perms(InsureeConfig.gql_mutation_update_insurees_perms):
                raise PermissionDenied(_("unauthorized"))
            if 'uuid' not in data:
                raise ValidationError(
                    "There is no uuid in updateMutation input!")
            # In DEBUG with anonymous, avoid accessing id_for_audit
            if user.is_anonymous and getattr(settings, 'DEBUG', False):
                data['audit_user_id'] = 0
            else:
                data['audit_user_id'] = user.id_for_audit
            # Default to in-place updates unless explicitly overridden
            data.setdefault('no_versioning', True)
            client_mutation_id = data.get("client_mutation_id")
            insuree = update_or_create_insuree(data, user)
            # Avoid passing AnonymousUser to mutation logger (causes UUID error)
            mutation_user = None if (user.is_anonymous and getattr(settings, 'DEBUG', False)) else user
            InsureeMutation.object_mutated(
                mutation_user, client_mutation_id=client_mutation_id, insuree=insuree)
            return None
        except Exception as exc:
            logger.exception("insuree.mutation.failed_to_update_insuree")
            return [{
                'message': _("insuree.mutation.failed_to_update_insuree"),
                'detail': str(exc)}
            ]


class DeleteInsureesMutation(OpenIMISMutation):
    """
    Delete one or several insurees.
    """
    _mutation_module = "insuree"
    _mutation_class = "DeleteInsureesMutation"

    class Input(OpenIMISMutation.Input):
        # family uuid, to 'lock' family while mutation is processed
        uuid = graphene.String(required=False)
        uuids = graphene.List(graphene.String)

    @classmethod
    def async_mutate(cls, user, **data):
        if not user.has_perms(InsureeConfig.gql_mutation_delete_insurees_perms):
            raise PermissionDenied(_("unauthorized"))
        errors = []
        for insuree_uuid in data["uuids"]:
            insuree = Insuree.objects \
                .prefetch_related('family') \
                .filter(uuid=UUID(str(insuree_uuid))) \
                .first()
            if insuree is None:
                errors.append({
                    'title': insuree_uuid,
                    'list': [{'message': _(
                        "insuree.validation.id_does_not_exist") % {'id': insuree_uuid}}]
                })
                continue
            if insuree.family and insuree.family.head_insuree.id == insuree.id:
                errors.append({
                    'title': insuree_uuid,
                    'list': [{'message': _(
                        "insuree.validation.delete_head_insuree") % {'id': insuree_uuid}}]
                })
                continue
            errors += InsureeService(user).set_deleted(insuree)
        if len(errors) == 1:
            errors = errors[0]['list']
        return errors


class RemoveInsureesMutation(OpenIMISMutation):
    """
    Delete one or several insurees.
    """
    _mutation_module = "insuree"
    _mutation_class = "RemoveInsureesMutation"

    class Input(OpenIMISMutation.Input):
        uuid = graphene.String()
        uuids = graphene.List(graphene.String)
        cancel_policies = graphene.Boolean(default_value=False)

    @classmethod
    def async_mutate(cls, user, **data):
        if not user.has_perms(InsureeConfig.gql_mutation_delete_insurees_perms):
            raise PermissionDenied(_("unauthorized"))
        errors = []
        for insuree_uuid in data["uuids"]:
            insuree = Insuree.objects \
                .prefetch_related('family') \
                .filter(uuid=(insuree_uuid)) \
                .first()
            if insuree is None:
                errors += {
                    'title': insuree_uuid,
                    'list': [{'message': _(
                        "insuree.validation.id_does_not_exist") % {'id': insuree_uuid}}]
                }
                continue
            if insuree.family.head_insuree.id == insuree.id:
                errors.append({
                    'title': insuree_uuid,
                    'list': [{'message': _(
                        "insuree.validation.remove_head_insuree") % {'id': insuree_uuid}}]
                })
                continue
            insuree_service = InsureeService(user)
            if data['cancel_policies']:
                errors += insuree_service.cancel_policies(insuree)
            errors += insuree_service.remove(insuree)
        if len(errors) == 1:
            errors = errors[0]['list']
        return errors


class SetFamilyHeadMutation(OpenIMISMutation):
    """
    Set (change) the family head insuree
    """
    _mutation_module = "insuree"
    _mutation_class = "SetFamilyHeadMutation"

    class Input(OpenIMISMutation.Input):
        uuid = graphene.String()
        insuree_uuid = graphene.String()

    @classmethod
    def async_mutate(cls, user, **data):
        if not user.has_perms(InsureeConfig.gql_mutation_update_families_perms):
            raise PermissionDenied(_("unauthorized"))
        try:
            family = Family.objects.get(uuid=(data['uuid']))
            insuree = Insuree.objects.get(uuid=(data['insuree_uuid']))
            family.save_history()
            prev_head = family.head_insuree
            if prev_head:
                prev_head.save_history()
                prev_head.head = False
                prev_head.save()
            family.head_insuree = insuree
            family.save()
            insuree.save_history()
            insuree.head = True
            insuree.save()
            return None
        except Exception as exc:
            logger.exception("insuree.mutation.failed_to_set_head_insuree")
            return [{
                'message': _("insuree.mutation.failed_to_set_head_insuree"),
                'detail': str(exc)}
            ]


class ChangeInsureeFamilyMutation(OpenIMISMutation):
    """
    Set (change) the family of an insuree
    """
    _mutation_module = "insuree"
    _mutation_class = "ChangeInsureeFamilyMutation"

    class Input(OpenIMISMutation.Input):
        family_uuid = graphene.String()
        insuree_uuid = graphene.String()
        cancel_policies = graphene.Boolean(default_value=False)

    @classmethod
    def async_mutate(cls, user, **data):
        if not user.has_perms(InsureeConfig.gql_mutation_update_families_perms) or \
                not user.has_perms(InsureeConfig.gql_mutation_update_insurees_perms):
            raise PermissionDenied(_("unauthorized"))
        try:
            family = Family.objects.get(uuid=(data['family_uuid']))
            insuree = Insuree.objects.get(uuid=(data['insuree_uuid']))
            insuree.save_history()
            insuree.family = family
            insuree.save()

            if data['cancel_policies']:
                InsureeService(user).cancel_policies(insuree)

            # Assign all the valid policies from the new family
            InsureePolicyService(user).add_insuree_policy(insuree)

            return None
        except Exception as exc:
            logger.exception(
                "insuree.mutation.failed_to_change_insuree_family")
            return [{
                'message': _("insuree.mutation.failed_to_change_insuree_family"),
                'detail': str(exc)}
            ]

class DeleteInsureeCheckInMutation(OpenIMISMutation):
    """
    delete insuree from checkin list
    """
    _mutation_module = "insuree"
    _mutation_class = "DeleteInsureeCheckInMutation"

    class Input(OpenIMISMutation.Input):
        insuree_uuid = graphene.String()

    @classmethod
    def async_mutate(cls, user, **data):
        if not user.has_perms(InsureeConfig.gql_mutation_delete_checkin_insuree_perms):
            raise PermissionDenied(_("unauthorized"))
        try:
            insuree = Insuree.objects.get(uuid=(data['insuree_uuid']))
            checkindata = InsureeCheckIn.objects.filter(insuree=insuree).order_by('-check_in_date').first()
            checkindata.delete()
            return None
        except Exception as exc:
            logger.exception(
                "insuree.mutation.failed_to_change_insuree_family")
            return [{
                'message': _("insuree.mutation.failed_to_change_insuree_family"),
                'detail': str(exc)}
            ]