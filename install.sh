#!/usr/bin/env bash

set -euo pipefail

die() {
    printf 'AMessenger installation failed: %s\n' "$*" >&2
    exit 1
}

usage() {
    printf 'Usage: %s [-p profile] [--repo url] [--relay url]\n' "$0" >&2
    printf '       %s --owner-chat <platform>:<chat id> [--agent name] [--kind corporate|personal] [--key key]\n' "$0" >&2
    exit 2
}

# The repository this installer is published from, so a new Owner runs one line
# and supplies no address. --repo and AMESSENGER_REPOSITORY_URL still win. Keep
# it equal to REPOSITORY_URL in amessenger/defaults.py; a test compares them.
DEFAULT_REPOSITORY_URL='https://github.com/jahoroshi/agent-messenger-plugin.git'

PROFILE=''
REPOSITORY_URL=''
RELAY_URL=''
RELAY_SUPPLIED=0
OWNER_CHAT=''
AGENT_NAME=''
AGENT_KIND='corporate'
OWNER_KEY=''
while [[ $# -gt 0 ]]; do
    case "$1" in
        -p)
            [[ $# -ge 2 && -n "$2" ]] || die "option -p needs a non-empty profile name; use -p <profile>"
            PROFILE=$2
            shift 2
            ;;
        --repo)
            [[ $# -ge 2 && -n "$2" ]] || die "option --repo needs a non-empty URL; use --repo <url>"
            REPOSITORY_URL=$2
            shift 2
            ;;
        --repo=*)
            REPOSITORY_URL=${1#--repo=}
            [[ -n "$REPOSITORY_URL" ]] || die "option --repo needs a non-empty URL; use --repo <url>"
            shift
            ;;
        --relay)
            [[ $# -ge 2 && -n "$2" ]] || die "option --relay needs a non-empty URL; use --relay <url>"
            RELAY_URL=$2
            RELAY_SUPPLIED=1
            shift 2
            ;;
        --relay=*)
            RELAY_URL=${1#--relay=}
            [[ -n "$RELAY_URL" ]] || die "option --relay needs a non-empty URL; use --relay <url>"
            RELAY_SUPPLIED=1
            shift
            ;;
        --owner-chat)
            [[ $# -ge 2 && -n "$2" ]] || die "option --owner-chat needs a value; use --owner-chat <platform>:<chat id>"
            OWNER_CHAT=$2
            shift 2
            ;;
        --agent)
            [[ $# -ge 2 && -n "$2" ]] || die "option --agent needs a name; use --agent <name>"
            AGENT_NAME=$2
            shift 2
            ;;
        --kind)
            [[ $# -ge 2 && -n "$2" ]] || die "option --kind needs corporate or personal"
            AGENT_KIND=$2
            shift 2
            ;;
        --key)
            [[ $# -ge 2 && -n "$2" ]] || die "option --key needs the Owner key"
            OWNER_KEY=$2
            shift 2
            ;;
        --)
            shift
            [[ $# -eq 0 ]] || die "unexpected argument '$1'; use only -p <profile>, --repo <url>, and --relay <url>"
            ;;
        -*) usage ;;
        *) die "unexpected argument '$1'; use only -p <profile>, --repo <url>, and --relay <url>" ;;
    esac
done

HERMES_BIN=$(command -v hermes || true)
[[ -n "$HERMES_BIN" ]] || die "Hermes was not found on PATH; install Hermes, then run this installer again."

resolve_profile() {
    [[ -n "$PROFILE" ]] && return

    local hermes_home active_profile profiles_root
    hermes_home=${HERMES_HOME:-${HOME:-}/.hermes}
    hermes_home=${hermes_home%/}

    # HERMES_HOME may already be pinned to a named profile. The path itself
    # is the only reliable profile signal in that form; HERMES_PROFILE is not
    # consulted because Hermes does not use it to select its config home.
    case "$hermes_home" in
        */profiles/*)
            active_profile=${hermes_home##*/profiles/}
            if [[ -n "$active_profile" && "$active_profile" != */* ]]; then
                PROFILE=$active_profile
                return
            fi
            ;;
    esac

    profiles_root="$hermes_home/profiles"
    if [[ -d "$profiles_root" ]]; then
        active_profile=''
        if [[ -f "$hermes_home/active_profile" ]]; then
            active_profile=$(<"$hermes_home/active_profile")
            active_profile="${active_profile#${active_profile%%[![:space:]]*}}"
            active_profile="${active_profile%${active_profile##*[![:space:]]}}"
        fi
        [[ -n "$active_profile" ]] || die "HERMES_HOME points to a profiles root but no active profile is resolvable; supply -p <profile> or set the active profile."
        [[ "$active_profile" =~ ^[a-z0-9][a-z0-9_-]{0,63}$ ]] || die "the active profile '$active_profile' is invalid or cannot be resolved; supply -p <profile>."
        if [[ "$active_profile" != default && ! -d "$profiles_root/$active_profile" ]]; then
            die "the active profile '$active_profile' does not exist under $profiles_root; supply -p <profile> or create that profile."
        fi
        PROFILE=$active_profile
        return
    fi

    # A single-profile/custom Hermes home has no profiles root to disambiguate.
    # Honor its sticky active profile when present and otherwise use Hermes's
    # normal default profile.
    if [[ -f "$hermes_home/active_profile" ]]; then
        active_profile=$(<"$hermes_home/active_profile")
        active_profile="${active_profile#${active_profile%%[![:space:]]*}}"
        active_profile="${active_profile%${active_profile##*[![:space:]]}}"
        [[ -n "$active_profile" ]] && PROFILE=$active_profile
    fi
    PROFILE=${PROFILE:-default}
}

resolve_profile
HERMES=("$HERMES_BIN" -p "$PROFILE")
PROFILE_DESCRIPTION="profile '$PROFILE'"

# Asking Hermes for the path makes profile resolution follow Hermes's own
# active-profile and HERMES_HOME rules. It also fails before any installation
# changes are made when an explicitly selected profile does not exist.
CONFIG_PATH=$(
    "${HERMES[@]}" config path </dev/null 2>/dev/null
) || die "could not open $PROFILE_DESCRIPTION; check the profile name and create the profile before installing AMessenger."

[[ "$CONFIG_PATH" == */config.yaml ]] || die "Hermes returned an invalid config path for $PROFILE_DESCRIPTION; check the Hermes installation."
PROFILE_HOME=${CONFIG_PATH%/config.yaml}
[[ -d "$PROFILE_HOME" ]] || die "the directory for $PROFILE_DESCRIPTION does not exist; create the profile before installing AMessenger."

PLUGINS_DIR="$PROFILE_HOME/plugins"
PLUGIN_PATH="$PLUGINS_DIR/amessenger"
mkdir -p "$PLUGINS_DIR" || die "could not create $PLUGINS_DIR; check its permissions, then rerun the installer."

# When the script is run from this checkout, BASH_SOURCE points at the real
# install.sh. A process-substitution download points at /dev/fd instead, so
# it follows the Git install path below.
CHECKOUT_PLUGIN=''
SCRIPT_SOURCE=${BASH_SOURCE[0]-}
if [[ -n "$SCRIPT_SOURCE" && "$SCRIPT_SOURCE" != /dev/fd/* && "$SCRIPT_SOURCE" != /proc/self/fd/* ]]; then
    SCRIPT_DIR=$(cd -- "$(dirname -- "$SCRIPT_SOURCE")" 2>/dev/null && pwd -P) || SCRIPT_DIR=''
    if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/amessenger/plugin.yaml" ]]; then
        CHECKOUT_PLUGIN="$SCRIPT_DIR/amessenger"
    fi
fi

if [[ -n "$CHECKOUT_PLUGIN" ]]; then
    if [[ -L "$PLUGIN_PATH" ]]; then
        LINK_TARGET=$(cd -- "$PLUGIN_PATH" 2>/dev/null && pwd -P) || die "the existing AMessenger link is broken; remove it or repair it, then rerun the installer."
        [[ "$LINK_TARGET" == "$CHECKOUT_PLUGIN" ]] || die "${PLUGIN_PATH} already points to a different plugin; resolve it before installing this checkout."
        printf 'AMessenger checkout is already linked for %s; keeping the existing installation.\n' "$PROFILE_DESCRIPTION"
    elif [[ -e "$PLUGIN_PATH" ]]; then
        die "${PLUGIN_PATH} already exists and is not the requested checkout link; move it aside or install from Git instead."
    else
        ln -s "$CHECKOUT_PLUGIN" "$PLUGIN_PATH" || die "could not link the checkout into ${PLUGIN_PATH}; check its permissions, then rerun the installer."
        printf 'Linked the AMessenger checkout for %s.\n' "$PROFILE_DESCRIPTION"
    fi
else
    REPOSITORY_URL=${REPOSITORY_URL:-${AMESSENGER_REPOSITORY_URL:-$DEFAULT_REPOSITORY_URL}}
    [[ -n "$REPOSITORY_URL" ]] || die "no AMessenger repository URL was supplied; use --repo <url> or set AMESSENGER_REPOSITORY_URL."
    if [[ -e "$PLUGIN_PATH" || -L "$PLUGIN_PATH" ]]; then
        [[ -d "$PLUGIN_PATH" && -f "$PLUGIN_PATH/plugin.yaml" ]] || die "${PLUGIN_PATH} exists but is not a complete AMessenger plugin; repair it or remove it before rerunning."
        printf 'AMessenger is already installed for %s; keeping the existing installation.\n' "$PROFILE_DESCRIPTION"
    else
        "${HERMES[@]}" plugins install "${REPOSITORY_URL}" </dev/null >/dev/null 2>&1 \
            || die "could not install AMessenger from Git; check Git access and the repository URL, then rerun the installer."
        [[ -d "$PLUGIN_PATH" && -f "$PLUGIN_PATH/plugin.yaml" ]] || die "Hermes reported that AMessenger was installed, but ${PLUGIN_PATH} is missing or incomplete; check the plugin installation and rerun the installer."
        printf 'Installed AMessenger from Git for %s.\n' "$PROFILE_DESCRIPTION"
    fi
fi

# Run the explicit enable command for both checkout and Git installations. The
# no-tool-override flag belongs to this subcommand, not to `plugins install`.
"${HERMES[@]}" plugins enable amessenger --no-allow-tool-override </dev/null >/dev/null 2>&1 \
    || die "AMessenger is present but could not be enabled; run the installer again after checking the profile's plugin directory."
printf 'AMessenger plugin is enabled for %s.\n' "$PROFILE_DESCRIPTION"

profile_file_mode() {
    local path=$1 mode
    if mode=$(stat -c '%a' -- "$path" 2>/dev/null); then
        printf '%s\n' "$mode"
        return 0
    fi
    if mode=$(stat -f '%Lp' "$path" 2>/dev/null); then
        printf '%s\n' "$mode"
        return 0
    fi
    return 1
}

RELAY_TEMP=''
cleanup_relay_temp() {
    if [[ -n "$RELAY_TEMP" && -e "$RELAY_TEMP" ]]; then
        rm -f -- "$RELAY_TEMP"
    fi
}
trap cleanup_relay_temp EXIT

write_profile_relay() {
    local env_path="$PROFILE_HOME/.env"
    local reference_path='' env_mode line candidate ending read_status
    local found=0 last_byte

    [[ "$RELAY_URL" != *$'\n'* && "$RELAY_URL" != *$'\r'* ]] \
        || die "option --relay cannot contain a newline"

    if [[ -e "$env_path" ]]; then
        [[ -f "$env_path" && -r "$env_path" ]] \
            || die "the profile .env exists but cannot be read; fix its permissions, then rerun the installer."
        reference_path=$env_path
    elif [[ -e "$CONFIG_PATH" ]]; then
        reference_path=$CONFIG_PATH
    else
        for candidate in "$PROFILE_HOME"/*; do
            if [[ -f "$candidate" ]]; then
                reference_path=$candidate
                break
            fi
        done
    fi

    if [[ -n "$reference_path" ]]; then
        env_mode=$(profile_file_mode "$reference_path") \
            || die "could not determine the permissions for the profile .env; fix the profile files, then rerun the installer."
    else
        env_mode=600
    fi

    RELAY_TEMP=$(mktemp "$PROFILE_HOME/.env.XXXXXX") \
        || die "could not create a temporary profile .env; check its permissions, then rerun the installer."

    if [[ -f "$env_path" ]]; then
        while :; do
            if IFS= read -r line; then
                read_status=0
            else
                read_status=$?
            fi
            if (( read_status != 0 )) && [[ -z "$line" ]]; then
                break
            fi

            candidate=$line
            ending=''
            if [[ "$candidate" == *$'\r' ]]; then
                candidate=${candidate%$'\r'}
                ending=$'\r'
            fi
            if (( read_status == 0 )); then
                ending="${ending}"$'\n'
            fi

            if [[ "$candidate" =~ ^[[:blank:]]*(export[[:blank:]]+)?AMESSENGER_URL[[:blank:]]*= ]]; then
                if (( found == 0 )); then
                    printf 'AMESSENGER_URL=%s%s' "$RELAY_URL" "$ending" >> "$RELAY_TEMP"
                    found=1
                fi
            else
                printf '%s%s' "$candidate" "$ending" >> "$RELAY_TEMP"
            fi
        done < "$env_path" \
            || die "could not read the profile .env; fix its permissions, then rerun the installer."
    fi

    if (( found == 0 )); then
        if [[ -s "$RELAY_TEMP" ]]; then
            last_byte=$(tail -c 1 "$RELAY_TEMP") \
                || die "could not inspect the profile .env before appending AMESSENGER_URL."
            if [[ -n "$last_byte" && "$last_byte" != $'\r' ]]; then
                printf '\n' >> "$RELAY_TEMP"
            fi
        fi
        printf 'AMESSENGER_URL=%s\n' "$RELAY_URL" >> "$RELAY_TEMP"
    fi

    chmod "$env_mode" "$RELAY_TEMP" \
        || die "could not apply the profile file permissions to .env; check its permissions, then rerun the installer."
    mv -f -- "$RELAY_TEMP" "$env_path" \
        || die "could not save AMESSENGER_URL in the profile .env; check its permissions, then rerun the installer."
    RELAY_TEMP=''
}

verify_profile_relay() {
    local env_path="$PROFILE_HOME/.env"
    if ! grep -Fqx -- "AMESSENGER_URL=$RELAY_URL" "$env_path"; then
        die "could not verify AMESSENGER_URL read-back in $env_path; expected AMESSENGER_URL=$RELAY_URL was not found."
    fi
    printf 'AMESSENGER_URL was saved in the profile .env for %s.\n' "$PROFILE_DESCRIPTION"
}

if (( RELAY_SUPPLIED )); then
    write_profile_relay
    verify_profile_relay
fi

# Hermes owns config.yaml's shape. Do not edit it directly: config set creates
# gateway/platforms/amessenger when a demo profile has no gateway block yet.
"${HERMES[@]}" config set gateway.platforms.amessenger.enabled true </dev/null >/dev/null 2>&1 \
    || die "could not enable the AMessenger platform in Hermes config; check that the profile config is writable, then rerun the installer."

PLATFORM_ENABLED=$("${HERMES[@]}" config get gateway.platforms.amessenger.enabled </dev/null 2>/dev/null) \
    || die "could not verify the AMessenger platform setting; run Hermes config get for the profile and repair it before retrying."
[[ "$PLATFORM_ENABLED" == 'true' ]] || die "Hermes did not save gateway.platforms.amessenger.enabled=true; repair the profile config before typing /amsg setup."
printf 'AMessenger platform is enabled for %s.\n' "$PROFILE_DESCRIPTION"

profile_suffix=''
case "$PROFILE_HOME" in
    */profiles/*)
        profile_suffix=${PROFILE_HOME##*/profiles/}
        [[ "$profile_suffix" != */* && -n "$profile_suffix" ]] || profile_suffix=''
        ;;
esac

gateway_service_exists() {
    local service_name plist_name task_name startup_dir home_dir schtasks_cmd
    service_name='hermes-gateway'
    plist_name='ai.hermes.gateway'
    task_name='Hermes_Gateway'
    home_dir=${HOME:-}
    if [[ -n "$profile_suffix" ]]; then
        service_name="hermes-gateway-$profile_suffix"
        plist_name="ai.hermes.gateway-$profile_suffix"
        task_name="Hermes_Gateway_$profile_suffix"
    fi

    case "$(uname -s)" in
        Linux*)
            command -v systemctl >/dev/null 2>&1 || return 1
            [[ -n "$home_dir" ]] || return 1
            [[ -f "$home_dir/.config/systemd/user/$service_name.service" || -f "/etc/systemd/system/$service_name.service" ]]
            ;;
        Darwin*)
            [[ -n "$home_dir" ]] || return 1
            [[ -f "$home_dir/Library/LaunchAgents/$plist_name.plist" ]]
            ;;
        MINGW*|MSYS*|CYGWIN*)
            schtasks_cmd=$(command -v schtasks.exe || command -v schtasks || true)
            if [[ -n "$schtasks_cmd" ]] && "$schtasks_cmd" /Query /TN "$task_name" >/dev/null 2>&1; then
                return 0
            fi
            startup_dir=${APPDATA:-}
            [[ -n "$startup_dir" ]] || startup_dir=${USERPROFILE:-}
            [[ -n "$startup_dir" ]] || return 1
            startup_dir="$startup_dir/Microsoft/Windows/Start Menu/Programs/Startup"
            [[ -f "$startup_dir/$task_name.vbs" || -f "$startup_dir/$task_name.cmd" ]]
            ;;
        *)
            return 1
            ;;
    esac
}

if gateway_service_exists; then
    "${HERMES[@]}" gateway restart </dev/null >/dev/null 2>&1 \
        || die "the gateway service exists but could not be restarted; run ${HERMES[*]} gateway restart and follow its repair instructions."
    printf 'Restarted the Hermes gateway service for %s.\n' "$PROFILE_DESCRIPTION"
else
    printf 'No gateway service is installed; restart the gateway before typing /amsg setup.\n'
fi

if [[ -n "$OWNER_CHAT" ]]; then
    # Finish here. /amsg setup reads the Owner Chat off a gateway event, and some
    # gateways hand the plugin no chat id at all -- on those the chat command can
    # never work, and the Owner is left with no way forward.
    AGENT_NAME=${AGENT_NAME:-$PROFILE}
    provision_args=(--owner-chat "$OWNER_CHAT" --agent "$AGENT_NAME" --kind "$AGENT_KIND")
    [[ -n "$OWNER_KEY" ]] && provision_args+=(--key "$OWNER_KEY")
    HERMES_HOME="$PROFILE_HOME" python3 "$PLUGIN_PATH/provision.py" "${provision_args[@]}" \
        || die "AMessenger is installed but could not be configured; the message above names the cause."
    printf 'AMessenger is installed and configured for %s. Restart the gateway and mail arrives in that chat.\n' "$PROFILE_DESCRIPTION"
else
    printf 'Next step: type /amsg setup in the chat that should receive mail.\n'
    printf 'If that chat cannot be used, rerun this installer with --owner-chat <platform>:<chat id>.\n'
fi
