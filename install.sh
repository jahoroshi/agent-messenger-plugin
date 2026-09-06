#!/usr/bin/env bash

set -euo pipefail

die() {
    printf 'AMessenger installation failed: %s\n' "$*" >&2
    exit 1
}

usage() {
    printf 'Usage: %s [-p profile] [--repo url] [--relay url] [--ca-file pem]\n' "$0" >&2
    printf '       %s --owner-chat <platform>[:<chat id>] [--agent name] [--kind corporate|personal] [--key key]\n' "$0" >&2
    printf '       <platform> alone (google_chat) uses this Hermes home channel; the Agent name defaults to the Owner.\n' >&2
    printf '       --ca-file names the authority that signed the relay certificate, when it is not a public one.\n' >&2
    printf 'Exit: 0 ready, 1 failed, 2 wrong usage, 3 installed and one step left.\n' >&2
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
CA_FILE=''
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
            [[ $# -ge 2 && -n "$2" ]] || die "option --owner-chat needs a value; use --owner-chat <platform> or --owner-chat <platform>:<chat id>"
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
        --ca-file)
            [[ $# -ge 2 && -n "$2" ]] || die "option --ca-file needs a path; use --ca-file <pem file>"
            CA_FILE=$2
            shift 2
            ;;
        --ca-file=*)
            CA_FILE=${1#--ca-file=}
            [[ -n "$CA_FILE" ]] || die "option --ca-file needs a path; use --ca-file <pem file>"
            shift
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

# --- one place every Hermes call goes through --------------------------------
#
# Every Hermes call used to end in `>/dev/null 2>&1`, so a scanner CAUTION
# verdict, a missing dependency and a broken profile all produced the same
# sentence and none of the evidence. The output is kept instead and the last of
# it is shown when the call fails. Hermes is never given the Owner key, and
# provision.py prints only text it has already made safe, so what is shown here
# carries no secret.
HERMES_LOG=$(mktemp "${TMPDIR:-/tmp}/amessenger-install.XXXXXX") \
    || die "could not create a temporary file for the installation log."
cleanup_log() {
    [[ -n "${HERMES_LOG:-}" && -e "$HERMES_LOG" ]] && rm -f -- "$HERMES_LOG"
    [[ -n "${RELAY_TEMP:-}" && -e "$RELAY_TEMP" ]] && rm -f -- "$RELAY_TEMP"
    return 0
}
trap cleanup_log EXIT

show_last_output() {
    if [[ -s "$HERMES_LOG" ]]; then
        printf 'What Hermes reported:\n' >&2
        tail -n 20 -- "$HERMES_LOG" >&2
    fi
}

run_hermes() {
    : > "$HERMES_LOG"
    if "${HERMES[@]}" "$@" </dev/null >"$HERMES_LOG" 2>&1; then
        return 0
    fi
    return 1
}

incomplete() {
    # AMessenger is installed and something a person must do is left. This is
    # not a crash, and a script that treats it as one retries forever; it is
    # also not success, and printing "mail is working" here is the lie this
    # installer was rewritten to stop telling.
    printf 'AMessenger is installed, and one step is left.\n%s\n' "$*"
    exit 3
}

# --- preflight ---------------------------------------------------------------
#
# Everything that decides whether this can work, checked before anything is
# changed. A failure here costs nothing; the same failure after the plugin is
# installed leaves a profile half-configured.
PYTHON_BIN=$(command -v python3 || true)
[[ -n "$PYTHON_BIN" ]] || die "python3 was not found on PATH; AMessenger's configuration step needs it."

[[ -w "$PLUGINS_DIR" ]] || die "$PLUGINS_DIR is not writable; fix its permissions, then rerun the installer."
[[ -w "$PROFILE_HOME" ]] || die "the directory for $PROFILE_DESCRIPTION is not writable; fix its permissions, then rerun the installer."

if [[ -n "$OWNER_CHAT" ]]; then
    case "$OWNER_CHAT" in
        *' '*) die "option --owner-chat must be <platform> or <platform>:<chat id>, with no spaces." ;;
    esac
fi
if [[ -n "$AGENT_KIND" ]]; then
    case "$AGENT_KIND" in
        corporate|personal) ;;
        *) die "option --kind must be corporate or personal; '$AGENT_KIND' is neither." ;;
    esac
fi
if [[ -n "$CA_FILE" ]]; then
    [[ -r "$CA_FILE" ]] || die "the CA file '$CA_FILE' cannot be read; supply a readable PEM file, or omit --ca-file."
fi

printf 'AMessenger installation\n'
printf 'Profile: %s\n' "$PROFILE"
printf 'Profile directory: %s\n' "$PROFILE_HOME"

# --- install, or update in place ---------------------------------------------
#
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
        printf 'Plugin: the checkout is already linked; it is always current.\n'
    elif [[ -e "$PLUGIN_PATH" ]]; then
        die "${PLUGIN_PATH} already exists and is not the requested checkout link; move it aside or install from Git instead."
    else
        ln -s "$CHECKOUT_PLUGIN" "$PLUGIN_PATH" || die "could not link the checkout into ${PLUGIN_PATH}; check its permissions, then rerun the installer."
        printf 'Plugin: linked from this checkout.\n'
    fi
else
    REPOSITORY_URL=${REPOSITORY_URL:-${AMESSENGER_REPOSITORY_URL:-$DEFAULT_REPOSITORY_URL}}
    [[ -n "$REPOSITORY_URL" ]] || die "no AMessenger repository URL was supplied; use --repo <url> or set AMESSENGER_REPOSITORY_URL."
    if [[ -L "$PLUGIN_PATH" ]]; then
        # A link is somebody's checkout. Updating it would rewrite their work.
        printf 'Plugin: a linked checkout is installed; leaving it as it is.\n'
    elif [[ -e "$PLUGIN_PATH" ]]; then
        [[ -d "$PLUGIN_PATH" && -f "$PLUGIN_PATH/plugin.yaml" ]] || die "${PLUGIN_PATH} exists but is not a complete AMessenger plugin; repair it or remove it before rerunning."
        # Rerunning the installer used to keep whatever was there and say
        # nothing about what it was, so an Owner who ran the documented command
        # to pick up a fix kept the version that had the bug.
        if [[ -d "$PLUGIN_PATH/.git" ]]; then
            if run_hermes plugins update amessenger; then
                printf 'Plugin: updated from Git.\n'
            else
                show_last_output
                die "AMessenger is installed but could not be updated; the output above names the cause. The existing installation was left untouched."
            fi
        else
            printf 'Plugin: already installed, and not from Git, so it cannot be updated here.\n'
            printf 'To replace it: hermes -p %s plugins remove amessenger, then rerun this installer.\n' "$PROFILE"
        fi
    else
        if run_hermes plugins install "${REPOSITORY_URL}"; then
            printf 'Plugin: installed from Git.\n'
        else
            show_last_output
            die "could not install AMessenger from Git; the output above names the cause. Check Git access and the repository URL, then rerun the installer."
        fi
        [[ -d "$PLUGIN_PATH" && -f "$PLUGIN_PATH/plugin.yaml" ]] || die "Hermes reported that AMessenger was installed, but ${PLUGIN_PATH} is missing or incomplete; check the plugin installation and rerun the installer."
    fi
fi

# Which code is actually installed, not which code was meant to be. Every
# declared version has said 0.1.0 since the first release.
INSTALLED_REVISION=$(git -C "$PLUGIN_PATH" rev-parse --short HEAD 2>/dev/null || true)
[[ -n "$INSTALLED_REVISION" ]] && printf 'Revision: %s\n' "$INSTALLED_REVISION"

# Run the explicit enable command for both checkout and Git installations. The
# no-tool-override flag belongs to this subcommand, not to `plugins install`.
if run_hermes plugins enable amessenger --no-allow-tool-override; then
    printf 'Plugin: enabled.\n'
else
    show_last_output
    die "AMessenger is present but could not be enabled; the output above names the cause."
fi

# --- profile values ----------------------------------------------------------

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

write_profile_value() {
    # Replace or append one name=value in the profile .env, keeping every other
    # line and the file's own permissions.
    local name=$1 value=$2
    local env_path="$PROFILE_HOME/.env"
    local reference_path='' env_mode line candidate ending read_status
    local found=0 last_byte

    [[ "$value" != *$'\n'* && "$value" != *$'\r'* ]] \
        || die "a profile value cannot contain a newline"

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

            if [[ "$candidate" =~ ^[[:blank:]]*(export[[:blank:]]+)?"$name"[[:blank:]]*= ]]; then
                if (( found == 0 )); then
                    printf '%s=%s%s' "$name" "$value" "$ending" >> "$RELAY_TEMP"
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
                || die "could not inspect the profile .env before appending $name."
            if [[ -n "$last_byte" && "$last_byte" != $'\r' ]]; then
                printf '\n' >> "$RELAY_TEMP"
            fi
        fi
        printf '%s=%s\n' "$name" "$value" >> "$RELAY_TEMP"
    fi

    chmod "$env_mode" "$RELAY_TEMP" \
        || die "could not apply the profile file permissions to .env; check its permissions, then rerun the installer."
    mv -f -- "$RELAY_TEMP" "$env_path" \
        || die "could not save $name in the profile .env; check its permissions, then rerun the installer."
    RELAY_TEMP=''

    grep -Fqx -- "$name=$value" "$env_path" \
        || die "could not verify the $name read-back in $env_path; expected $name=$value was not found."
}

if (( RELAY_SUPPLIED )); then
    write_profile_value AMESSENGER_URL "$RELAY_URL"
    printf 'Relay: %s\n' "$RELAY_URL"
fi
if [[ -n "$CA_FILE" ]]; then
    write_profile_value AMESSENGER_CA_FILE "$CA_FILE"
    printf 'Relay certificate authority: %s\n' "$CA_FILE"
fi

# Hermes owns config.yaml's shape. Do not edit it directly: config set creates
# gateway/platforms/amessenger when a demo profile has no gateway block yet.
if ! run_hermes config set gateway.platforms.amessenger.enabled true; then
    show_last_output
    die "could not enable the AMessenger platform in Hermes config; check that the profile config is writable, then rerun the installer."
fi

if ! run_hermes config get gateway.platforms.amessenger.enabled; then
    show_last_output
    die "could not verify the AMessenger platform setting; repair the profile before retrying."
fi
PLATFORM_ENABLED=$(tail -n 1 -- "$HERMES_LOG" | tr -d '\r')
[[ "$PLATFORM_ENABLED" == 'true' ]] || die "Hermes did not save gateway.platforms.amessenger.enabled=true; repair the profile config before typing /amsg setup."
printf 'Platform: enabled.\n'

# --- configuration, before the restart ---------------------------------------
#
# This used to run after the gateway was restarted, so the gateway it restarted
# read the settings from before this step and the installer then asked for a
# second restart it called optional. One restart, and everything it needs to
# read is already written when it happens.
if [[ -n "$OWNER_CHAT" ]]; then
    [[ -f "$PLUGIN_PATH/provision.py" ]] \
        || die "the installed plugin has no provision.py; the installation is incomplete."
    provision_args=(--owner-chat "$OWNER_CHAT" --kind "$AGENT_KIND")
    # No --agent: provision.py names the Agent after the Owner the key belongs
    # to, which is unique already. Thirty Owners run one identical command.
    [[ -n "$AGENT_NAME" ]] && provision_args+=(--agent "$AGENT_NAME")
    [[ -n "$OWNER_KEY" ]] && provision_args+=(--key "$OWNER_KEY")
    [[ -n "$CA_FILE" ]] && provision_args+=(--ca-file "$CA_FILE")
    HERMES_HOME="$PROFILE_HOME" "$PYTHON_BIN" "$PLUGIN_PATH/provision.py" "${provision_args[@]}" \
        || die "AMessenger is installed but could not be configured; the message above names the cause."
fi

# --- one restart -------------------------------------------------------------

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

HEALTH_PATH="$PROFILE_HOME/amessenger/health.json"
RESTART_EPOCH=$(date -u +%s)

if gateway_service_exists; then
    if run_hermes gateway restart; then
        printf 'Gateway: restarted.\n'
    else
        show_last_output
        die "the gateway service exists but could not be restarted; the output above names the cause."
    fi
else
    # Never write a service. A host that runs its gateway in a terminal, in a
    # container, or under somebody else's supervisor is not broken, and guessing
    # wrong here leaves two gateways fighting over one inbox.
    incomplete "$(printf 'No gateway service is installed for %s, so nothing was restarted.\n\nStart the gateway:\nhermes -p %s gateway run' "$PROFILE_DESCRIPTION" "$PROFILE")"
fi

# --- wait for evidence, then report what is true ------------------------------
#
# "Installed" and "receiving mail" are different successes. The gateway writes a
# health record once its receive loop has actually reached the relay, so this
# waits for a record newer than the restart and lets the diagnostic read it.
record_is_fresh() {
    local written
    written=$(stat -c '%Y' -- "$HEALTH_PATH" 2>/dev/null) \
        || written=$(stat -f '%m' "$HEALTH_PATH" 2>/dev/null) \
        || return 1
    (( written >= RESTART_EPOCH ))
}

RUNTIME_WAIT_SECONDS=${AMESSENGER_INSTALL_WAIT_SECONDS:-90}
waited=0
while (( waited < RUNTIME_WAIT_SECONDS )); do
    if record_is_fresh; then
        break
    fi
    sleep 1
    waited=$((waited + 1))
done

if ! record_is_fresh; then
    incomplete "$(printf 'The gateway did not report its state within %ss.\n\nAsk it directly:\nHERMES_HOME=%s python3 %s' "$RUNTIME_WAIT_SECONDS" "$PROFILE_HOME" "$PLUGIN_PATH/doctor.py")"
fi

printf '\n'
set +e
HERMES_HOME="$PROFILE_HOME" "$PYTHON_BIN" "$PLUGIN_PATH/doctor.py"
DOCTOR_CODE=$?
set -e
printf '\n'
case "$DOCTOR_CODE" in
    0)
        printf 'AMessenger is ready. Messages sent to this Agent will arrive in the Owner Chat.\n'
        ;;
    3)
        printf 'AMessenger is installed, and one step is left. The report above names it.\n'
        printf 'If that step is the Owner Chat, type /amsg setup or /sethome in the chat that should receive Messages.\n'
        ;;
    *)
        printf 'AMessenger is installed and cannot carry mail yet. The report above names the fault.\n' >&2
        ;;
esac
exit "$DOCTOR_CODE"
