/** Compatibility entry for the local UM data controller.
 *
 *  The implementation is split into `./um-data/`; this path stays so existing
 *  callers keep importing `UmDataController` from here. */
export {
  UmDataController,
  type UmDataControllerOptions,
} from "./um-data/um-data-controller";
