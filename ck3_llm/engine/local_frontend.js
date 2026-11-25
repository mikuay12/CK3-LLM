// The two frontend actions have their own account/name checks before the
// native game-readiness check. The local backend supplies the player identity.
function installLocalFrontendGates(isLocalReady) {
    const actions = [
        { name: 'join', rva: 0x18923f2 },
        { name: 'host', rva: 0x189293a }
    ];
    for (const action of actions) {
        Interceptor.attach(base.add(action.rva), {
            onEnter() {
                if (isLocalReady()) {
                    this.context.rbx = this.context.rbx.and(ptr('0xffffffffffffff00'));
                    send({ event: 'local_frontend_action', action: action.name });
                }
            }
        });
    }
    send({ event: 'local_frontend_gates_installed', actions: actions.map(action => action.name) });
}
