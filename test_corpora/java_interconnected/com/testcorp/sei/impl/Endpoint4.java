package com.testcorp.sei.impl;

import javax.jws.WebMethod;
import javax.jws.WebService;
import com.testcorp.api.Handler;
import com.testcorp.api.HandlerImpl4;
import com.testcorp.manager.Manager4;
import com.testcorp.facade.Facade4;
import com.testcorp.service.Service24;
import com.testcorp.service.Service4;

@WebService
public class Endpoint4 {

    @WebMethod
    public String lookup(String id) throws Exception {
        return new Service4().handle(id);
    }

    @WebMethod
    public String dispatch(String id) throws Exception {
        Handler h = new HandlerImpl4();
        return h.run(id);
    }

    @WebMethod
    public String viaManager(String id) throws Exception {
        return new Manager4().process(id);
    }

    @WebMethod
    public String deepChain(String id) throws Exception {
        return new Manager4().deep(id);
    }

    @WebMethod
    public String deepest(String id) throws Exception {
        return new Facade4().orchestrate(id);
    }

    @WebMethod
    public String viaFacade(String id) throws Exception {
        return new Facade4().orchestrateDirect(id);
    }

    @WebMethod
    public String lookupAlt1(String id) throws Exception {
        return new Service24().handle(id);
    }
}
