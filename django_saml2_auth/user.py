"""Utility functions for getting or creating user accounts
"""

from datetime import datetime, timedelta
import logging
from typing import Any, Dict, Tuple, Type, Union, Optional
import copy

import jwt
from dictor import dictor
from django import get_version
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db.models import Model
from django.http import HttpRequest
from django_saml2_auth.errors import (CREATE_USER_ERROR, GROUP_JOIN_ERROR,
                                      SHOULD_NOT_CREATE_USER, NO_JWT_SECRET_OR_ALGORITHM,
                                      CANNOT_DECODE_JWT_TOKEN)
from django_saml2_auth.exceptions import SAMLAuthError
from django_saml2_auth.utils import run_hook
from jwt.exceptions import PyJWTError
from pkg_resources import parse_version

logger = logging.getLogger(__name__)


def create_new_user(email: str, firstname: str, lastname: str) -> Type[Model]:
    """Create a new user with the given information

    Args:
        email (str): Email
        firstname (str): First name
        lastname (str): Last name

    Raises:
        SAMLAuthError: There was an error creating the new user.
        SAMLAuthError: There was an error joining the user to the group.

    Returns:
        Type[Model]: Returns a new user object, usually a subclass of the the User model
    """
    user_model = get_user_model()

    is_active = dictor(settings.SAML2_AUTH, "NEW_USER_PROFILE.ACTIVE_STATUS", default=True)
    is_staff = dictor(settings.SAML2_AUTH, "NEW_USER_PROFILE.STAFF_STATUS", default=False)
    is_superuser = dictor(settings.SAML2_AUTH, "NEW_USER_PROFILE.SUPERUSER_STATUS", default=False)
    user_groups = dictor(settings.SAML2_AUTH, "NEW_USER_PROFILE.USER_GROUPS", default=[])

    try:
        user = user_model.objects.create_user_with_email_username(email, first_name=firstname, last_name=lastname)
        user.is_active = is_active
        user.is_staff = is_staff
        user.is_superuser = is_superuser
        user.save()
    except Exception as exc:
        logger.exception('There was an error creating the new user.')
        raise SAMLAuthError("There was an error creating the new user.", extra={
            "exc": exc,
            "exc_type": type(exc),
            "error_code": CREATE_USER_ERROR,
            "reason": "There was an error processing your request.",
            "status_code": 500
        })

    try:
        groups = [Group.objects.get(name=group) for group in user_groups]
        if groups:
            if parse_version(get_version()) <= parse_version("1.8"):
                user.groups = groups
            else:
                user.groups.set(groups)
    except Exception as exc:
        logger.exception('There was an error joining the user to the group.')
        raise SAMLAuthError("There was an error joining the user to the group.", extra={
            "exc": exc,
            "exc_type": type(exc),
            "error_code": GROUP_JOIN_ERROR,
            "reason": "There was an error processing your request.",
            "status_code": 500
        })

    user.save()
    user.refresh_from_db()

    return user


def get_or_create_user(request: HttpRequest, user: Dict[str, Any], extra_data: Optional[Dict[str, str]]) -> Tuple[bool, Type[Model]]:
    """Get or create a new user and optionally add it to one or more group(s)

    Args:
        user (Dict[str, Any]): User information

    Raises:
        SAMLAuthError: Cannot create user.

    Returns:
        Tuple[bool, Type[Model]]: A tuple containing user creation status and user object
    """
    user_model = get_user_model()
    created = False

    try:
        target_user = get_user(user)
    except user_model.DoesNotExist:
        should_create_new_user = settings.SAML2_AUTH.get("CREATE_USER", True)
        if should_create_new_user:
            target_user = create_new_user(get_user_id(user), user["first_name"], user["last_name"])

            create_user_trigger = dictor(settings.SAML2_AUTH, "TRIGGER.CREATE_USER")
            if create_user_trigger:
                run_hook(create_user_trigger, request, user, target_user, extra_data)

            target_user.refresh_from_db()
            created = True
        else:
            raise SAMLAuthError("Cannot create user.", extra={
                "exc_type": Exception,
                "error_code": SHOULD_NOT_CREATE_USER,
                "reason": "Due to current config, a new user should not be created.",
                "status_code": 500
            })

    # Optionally update this user's group assignments by updating group memberships from SAML groups
    # to Django equivalents
    group_attribute = dictor(settings.SAML2_AUTH, "ATTRIBUTES_MAP.groups")
    group_map = settings.SAML2_AUTH.get("GROUPS_MAP")

    if group_attribute and group_attribute in user["user_identity"]:
        groups = []

        for group_name in user["user_identity"][group_attribute]:
            # Group names can optionally be mapped to different names in Django
            if group_map and group_name in group_map:
                group_name_django = group_map[group_name]
            else:
                group_name_django = group_name

            try:
                groups.append(Group.objects.get(name=group_name_django))
            except Group.DoesNotExist:
                pass

        if parse_version(get_version()) >= parse_version("2.0"):
            for group in groups:
                target_user.groups.add(group)
        else:
            target_user.groups = groups

    return (created, target_user)


def get_user_id(user: Dict[str, str]) -> Optional[str]:
    """Get user_id (username or email) from user object

    Args:
        user (Dict[str, str]): A cleaned user info object

    Returns:
        Optional[str]: user_id, which is either email or username
    """
    user_model = get_user_model()
    user_id = None

    if isinstance(user, dict):
        user_id = user["email"] if user_model.USERNAME_FIELD == "email" else user["username"]

    if isinstance(user, str):
        user_id = user

    return user_id.lower()


def get_user(user: Union[str, Dict[str, str]]) -> Type[Model]:
    """Get user from database given a cleaned user info object or a user_id

    Args:
        user (Union[str, Dict[str, str]]): Either a user_id (as str) or a cleaned user info object

    Returns:
        Type[Model]: An instance of the User model
    """
    user_model = get_user_model()
    user_id = get_user_id(user)

    get_user_trigger = dictor(settings.SAML2_AUTH, "TRIGGER.GET_USER")
    if get_user_trigger:
        logger.debug('Calling get_user trigger %s', user_id)
        user = run_hook(get_user_trigger, user_id)
        if user is None:
            raise user_model.DoesNotExist
        return user

    # Should email be case-sensitive or not. Default is False (case-insensitive).
    login_case_sensitive = settings.SAML2_AUTH.get("LOGIN_CASE_SENSITIVE", False)
    id_field = (
        user_model.USERNAME_FIELD
        if login_case_sensitive
        else f"{user_model.USERNAME_FIELD}__iexact")
    return user_model.objects.get(**{id_field: user_id})


def create_jwt_token(user_id: str, **extra_data) -> Optional[str]:
    """Create a new JWT token

    Args:
        user_id (str): User's username or email based on User.USERNAME_FIELD

    Raises:
        SAMLAuthError: Cannot create JWT token. Specify secret and algorithm.

    Returns:
        Optional[str]: JWT token
    """
    user_model = get_user_model()

    jwt_secret = settings.SAML2_AUTH.get("JWT_SECRET")
    jwt_algorithm = settings.SAML2_AUTH.get("JWT_ALGORITHM")
    jwt_expiration = settings.SAML2_AUTH.get("JWT_EXP", 60)  # default: 1 minute
    payload = {
        user_model.USERNAME_FIELD: user_id,
        "exp": (datetime.utcnow() +
                timedelta(seconds=jwt_expiration)).timestamp()
    }
    payload.update(extra_data)

    if not jwt_secret or not jwt_algorithm:
        raise SAMLAuthError("Cannot create JWT token. Specify secret and algorithm.", extra={
            "exc_type": Exception,
            "error_code": NO_JWT_SECRET_OR_ALGORITHM,
            "reason": "Cannot create JWT token for login.",
            "status_code": 500
        })

    jwt_token = jwt.encode(payload, jwt_secret, algorithm=jwt_algorithm)
    return jwt_token


def decode_jwt_token(jwt_token: str) -> Tuple[Optional[str], Dict[str, str]]:
    """Decode a JWT token

    Args:
        jwt_token (str): The token to decode

    Raises:
        SAMLAuthError: Cannot decode JWT token.

    Returns:
        Tuple[Optional[str], Dict[str, str]]: A user_id as str or None, and dictionary wih extra data.
    """
    jwt_secret = settings.SAML2_AUTH.get("JWT_SECRET")
    jwt_algorithm = settings.SAML2_AUTH.get("JWT_ALGORITHM")

    try:
        data = jwt.decode(jwt_token, jwt_secret, algorithms=jwt_algorithm)
        user_model = get_user_model()
        extra_data = copy.copy(data)
        extra_data.pop('exp')
        return data[user_model.USERNAME_FIELD], extra_data
    except PyJWTError as exc:
        raise SAMLAuthError("Cannot decode JWT token.", extra={
            "exc": exc,
            "exc_type": type(exc),
            "error_code": CANNOT_DECODE_JWT_TOKEN,
            "reason": "Cannot decode JWT token.",
            "status_code": 500
        })
