docker() {
    printf '%s\t' "$PWD" "$@" >> "$DOCKER_TEST_LOG"
    printf '\n' >> "$DOCKER_TEST_LOG"
    if [[ "$1" == info ]]; then return 0; fi
    shift 3  # compose --project-directory ROOT
    if [[ "${2:-}" == --help ]]; then
        printf '%s\n' '--wait-timeout --ignore-buildable'
        return 0
    fi
    if [[ "$1" == "$DOCKER_TEST_FAIL" ]]; then return 19; fi
    return 0
}
